"""Structured, lightweight forward data-flow analysis for Python call sites.

The abstract domain tracks sets of constructor/reference candidates and an
explicit may-be-unbound bit. It distinguishes a call inside a branch from one
following the branch, merges both if/else outcomes, and respects assignment
order. This is NOT a full Python CFG, alias analysis, or C3 dispatch analysis.
"""
from __future__ import annotations

import ast
from copy import deepcopy
from typing import Any

from ast_parser import expr_name

# JSON-friendly binding. Each option is a possible abstract value, not a
# concrete runtime object. Explicit source lines support evidence inspection.


def _value(kind: str, name: str | None = None, line: int | None = None) -> dict:
    return {"options": [{"kind": kind, "name": name, "line": line}], "may_be_unbound": False}


def _simple_annotation(node: ast.AST | None) -> str | None:
    """A single named type only. No guesses for Union, generic, Any or calls.

    This never evaluates the annotation (including forward-reference strings).
    """
    if isinstance(node, (ast.Name, ast.Attribute)):
        return expr_name(node)
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        try:
            expr = ast.parse(node.value, mode='eval').body
        except SyntaxError:
            return None
        if isinstance(expr, (ast.Name, ast.Attribute)):
            return expr_name(expr)
    return None


def _merge_value(left: dict | None, right: dict | None) -> dict:
    options = []
    for value in (left, right):
        if value:
            for opt in value.get('options', []):
                if opt not in options:
                    options.append(opt)
    result = {"options": options, "may_be_unbound": (left is None or right is None or
            bool(left and left.get('may_be_unbound')) or bool(right and right.get('may_be_unbound')))}
    # Only preserve a collection type if every reachable alternative agrees.
    if left and right and left.get('collection') and left.get('collection') == right.get('collection'):
        result['collection'] = left['collection']
    return result


def _merge(left: dict[str, dict], right: dict[str, dict]) -> dict[str, dict]:
    return {k: _merge_value(left.get(k), right.get(k)) for k in left.keys() | right.keys()}


class FlowAnalyzer:
    def __init__(self):
        self.snapshots: dict[tuple[int, int], dict] = {}

    @staticmethod
    def _elements(collection: dict, line: int | None) -> dict:
        """Unbox an abstract iterable without treating the list as a class."""
        options = []
        for option in collection.get('options', []):
            kind = option.get('kind')
            if kind == 'sequence_reference':
                options.append({**option, 'kind': 'reference', 'from_collection': True})
            elif kind == 'sequence_constructed':
                options.append({**option, 'kind': 'constructed', 'from_collection': True})
            elif kind in ('sequence_unknown',):
                options.append({**option, 'kind': 'unknown'})
        if not options:
            options = _value('unknown', line=line)['options']
        return {'options': options, 'may_be_unbound': bool(collection.get('may_be_unbound'))}

    def _comprehension(self, expr: ast.ListComp, state: dict[str, dict], *, record_calls: bool) -> dict:
        # Generator targets exist only in comprehension scope (Python 3).
        local = deepcopy(state)
        for gen in expr.generators:
            if record_calls:
                self._expr(gen.iter, local)
            itervalue = self._get(gen.iter, local)
            self._assign(gen.target, self._elements(itervalue, getattr(gen.target, "lineno", expr.lineno)), local)
            if record_calls:
                for condition in gen.ifs:
                    self._expr(condition, local)
        if record_calls:
            self._expr(expr.elt, local)
        element = self._get(expr.elt, local)
        result = {'options': [], 'may_be_unbound': False, 'collection': 'list'}
        for opt in element.get('options', []):
            kind = opt.get('kind')
            if kind == 'reference':
                result['options'].append({**opt, 'kind': 'sequence_reference'})
            elif kind == 'constructed':
                result['options'].append({**opt, 'kind': 'sequence_constructed'})
            else:
                result['options'].append({**opt, 'kind': 'sequence_unknown'})
        if element.get('may_be_unbound'):
            result['options'].append(_value('sequence_unknown', line=expr.lineno)['options'][0])
        return result

    def _get(self, expr: ast.AST | None, state: dict[str, dict]) -> dict:
        line = getattr(expr, 'lineno', None)
        if isinstance(expr, ast.Call):
            if isinstance(expr.func, ast.Name):
                bound = state.get(expr.func.id)
                if bound:
                    options = []
                    for opt in bound.get('options', []):
                        if opt.get('kind') == 'reference':
                            options.append({**opt, 'kind': 'constructed'})
                        else:
                            options.append(_value('unknown', line)['options'][0])
                    if bound.get('may_be_unbound'):
                        options.append(_value('unknown', line)['options'][0])
                    return {'options': options or _value('unknown', line)['options'],
                            'may_be_unbound': False}
            return _value('constructed', expr_name(expr.func), line)
        if isinstance(expr, ast.Name):
            # Capture the value NOW; future reassignment must not rewrite aliases.
            return deepcopy(state[expr.id]) if expr.id in state else _value('reference', expr.id, line)
        if isinstance(expr, ast.Attribute):
            return _value('reference', expr_name(expr), line)
        if isinstance(expr, (ast.List, ast.Tuple, ast.Set)):
            opts = []
            for item in expr.elts:
                if isinstance(item, ast.Starred):
                    sub = self._get(item.value, state)
                else:
                    sub = self._get(item, state)
                # 'sequence_reference' indicates a possible collection element.
                for opt in sub['options']:
                    kind = ('sequence_reference' if opt['kind'] in ('reference', 'sequence_reference')
                            else 'sequence_constructed' if opt['kind'] in ('constructed', 'sequence_constructed')
                            else 'sequence_unknown')
                    opts.append({**opt, 'kind': kind})
            return {'options': opts, 'may_be_unbound': False,
                    'collection': ('list' if isinstance(expr, ast.List) else
                                   'tuple' if isinstance(expr, ast.Tuple) else 'set')}
        if isinstance(expr, ast.Dict):
            opts = []
            for key, value in zip(expr.keys, expr.values):
                # Key-dependent lookup is supported only for literal keys.
                literal_key = key.value if isinstance(key, ast.Constant) and isinstance(key.value, (str, int)) else None
                val = self._get(value, state)
                for option in val['options']:
                    opts.append({**option, 'kind': 'dictionary_reference' if option['kind'] == 'reference' else
                                 'dictionary_unknown', 'key': literal_key})
            return {'options': opts, 'collection': 'dict', 'may_be_unbound': False}
        if isinstance(expr, ast.Subscript) and isinstance(expr.value, ast.Name):
            container = state.get(expr.value.id)
            if container and container.get('collection') == 'dict':
                key = expr.slice.value if isinstance(expr.slice, ast.Constant) else None
                choices = [opt for opt in container.get('options', [])
                           if key is None or opt.get('key') is None or opt.get('key') == key]
                options = [{k: v for k, v in opt.items() if k != 'key'} |
                           {'kind': 'reference' if opt['kind'] == 'dictionary_reference' else 'unknown'}
                           for opt in choices]
                if not choices or container.get('may_be_unbound'):
                    options.append(_value('unknown', line)['options'][0])
                return {'options': options, 'may_be_unbound': False}
        if isinstance(expr, ast.ListComp):
            return self._comprehension(expr, state, record_calls=False)
        return _value('unknown', line=line)

    def _expr(self, expr: ast.AST | None, state: dict[str, dict]) -> None:
        if expr is None:
            return
        if isinstance(expr, (ast.Lambda, ast.FunctionDef, ast.AsyncFunctionDef)):
            return
        if isinstance(expr, ast.ListComp):
            self._comprehension(expr, state, record_calls=True)
            return
        if isinstance(expr, ast.Call):
            func = expr.func
            name = expr_name(func)
            var = name.split('.', 1)[0] if name else None
            if var:
                self.snapshots[(expr.lineno, expr.col_offset)] = {
                    'variable': var, 'binding': deepcopy(state.get(var)),
                    'tracked': True}
            # Track mutations only on objects proven to be a local list.
            # This is a may-element set, not a precise sequence ordering.
            if (isinstance(func, ast.Attribute) and func.attr == 'append' and
                    isinstance(func.value, ast.Name) and expr.args):
                receiver = func.value.id
                old = state.get(receiver)
                if old and old.get('collection') == 'list':
                    self._expr(expr.args[0], state)
                    value = self._get(expr.args[0], state)
                    for opt in value['options']:
                        kind = ('sequence_reference' if opt['kind'] == 'reference' else
                                'sequence_constructed' if opt['kind'] == 'constructed' else
                                'sequence_unknown')
                        updated = {**opt, 'kind': kind}
                        if updated not in old['options']:
                            old['options'].append(updated)
                    if value.get('may_be_unbound'):
                        old['options'].append(_value('sequence_unknown', line=expr.lineno)['options'][0])
                    for arg in expr.args[1:]:
                        self._expr(arg, state)
                    for kw in expr.keywords:
                        self._expr(kw.value, state)
                    return
            if (isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name) and
                    func.attr in {'clear', 'extend', 'insert', 'pop', 'remove', 'reverse', 'sort'}):
                # Untreated mutations must not leave a stale definite set of
                # class elements behind. Keep the type only as an uncertainty.
                receiver = func.value.id
                if state.get(receiver, {}).get('collection') == 'list':
                    state[receiver] = {'options': [_value('sequence_unknown', expr.lineno)['options'][0]],
                                       'collection': 'list', 'may_be_unbound': False}
        if isinstance(expr, ast.NamedExpr):
            self._expr(expr.value, state)
            self._assign(expr.target, self._get(expr.value, state), state)
            return
        # Calls within the expression are evaluated in source order for our
        # supported subset. Expression short-circuiting is not fully modeled.
        for child in ast.iter_child_nodes(expr):
            self._expr(child, state)

    def _assign(self, target: ast.AST, value: dict, state: dict[str, dict]) -> None:
        if isinstance(target, ast.Name):
            state[target.id] = deepcopy(value)
        elif isinstance(target, ast.Starred):
            self._assign(target.value, _value('unknown', line=getattr(target, 'lineno', None)), state)
        elif isinstance(target, (ast.Tuple, ast.List)):
            # Only precise destructuring from a literal tuple/list is covered
            # by _statement below; otherwise invalidate targets conservatively.
            for elt in target.elts:
                self._assign(elt, _value('unknown', line=getattr(elt, 'lineno', None)), state)

    def _function(self, fn: ast.FunctionDef | ast.AsyncFunctionDef, inherited: dict[str, dict]) -> None:
        # Free variables are inherited for simple closures. Locals shadow them.
        state = deepcopy(inherited)
        args = fn.args
        for arg in [*args.posonlyargs, *args.args, *args.kwonlyargs]:
            # Annotation describes an expected object type, not a runtime
            # guarantee. The resolver marks resulting edges as MAY_CALL.
            hint = _simple_annotation(arg.annotation)
            state[arg.arg] = (_value('annotated_parameter', hint, line=arg.lineno)
                              if hint and arg.arg not in ('self', 'cls')
                              else _value('unknown', line=arg.lineno))
        if args.vararg:
            state[args.vararg.arg] = _value('unknown', line=args.vararg.lineno)
        if args.kwarg:
            state[args.kwarg.arg] = _value('unknown', line=args.kwarg.lineno)
        self._block(fn.body, state)

    def _statement(self, stmt: ast.stmt, state: dict[str, dict]) -> tuple[dict[str, dict], bool]:
        if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for dec in stmt.decorator_list:
                self._expr(dec, state)
            for default in [*stmt.args.defaults, *stmt.args.kw_defaults]:
                self._expr(default, state)
            self._function(stmt, state)
            state[stmt.name] = _value('reference', stmt.name, stmt.lineno)
            return state, True
        if isinstance(stmt, ast.ClassDef):
            for base in stmt.bases:
                self._expr(base, state)
            # Method definitions belong to the class but their calls are
            # executed in separate function scopes.
            class_state = deepcopy(state)
            for child in stmt.body:
                if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    self._function(child, class_state)
                else:
                    self._statement(child, class_state)
            state[stmt.name] = _value('reference', stmt.name, stmt.lineno)
            return state, True
        if isinstance(stmt, ast.Assign):
            self._expr(stmt.value, state)
            bound = self._get(stmt.value, state)
            for target in stmt.targets:
                if isinstance(target, (ast.List, ast.Tuple)) and isinstance(stmt.value, (ast.List, ast.Tuple)) and len(target.elts) == len(stmt.value.elts):
                    for t, v in zip(target.elts, stmt.value.elts):
                        self._assign(t, self._get(v, state), state)
                else:
                    self._assign(target, bound, state)
            return state, True
        if isinstance(stmt, ast.AnnAssign):
            self._expr(stmt.value, state)
            self._assign(stmt.target, self._get(stmt.value, state), state)
            return state, True
        if isinstance(stmt, ast.AugAssign):
            self._expr(stmt.value, state)
            self._assign(stmt.target, _value('unknown', line=stmt.lineno), state)
            return state, True
        if isinstance(stmt, ast.If):
            self._expr(stmt.test, state)
            true_state, true_reaches = self._block(stmt.body, deepcopy(state))
            false_state, false_reaches = self._block(stmt.orelse, deepcopy(state))
            if true_reaches and false_reaches:
                return _merge(true_state, false_state), True
            if true_reaches:
                return true_state, True
            if false_reaches:
                return false_state, True
            return state, False
        if isinstance(stmt, (ast.For, ast.AsyncFor)):
            self._expr(stmt.iter, state)
            body_state = deepcopy(state)
            iterable = self._get(stmt.iter, state)
            iter_value = self._elements(iterable, stmt.lineno)
            self._assign(stmt.target, iter_value, body_state)
            body_state, _ = self._block(stmt.body, body_state)
            # Loop may run zero times. Input-state branch remains reachable.
            merged = _merge(state, body_state)
            after_else, _ = self._block(stmt.orelse, deepcopy(merged))
            return _merge(merged, after_else), True
        if isinstance(stmt, ast.While):
            self._expr(stmt.test, state)
            body_state, _ = self._block(stmt.body, deepcopy(state))
            merged = _merge(state, body_state)
            else_state, _ = self._block(stmt.orelse, deepcopy(merged))
            return _merge(merged, else_state), True
        if isinstance(stmt, (ast.With, ast.AsyncWith)):
            for item in stmt.items:
                self._expr(item.context_expr, state)
                if item.optional_vars:
                    self._assign(item.optional_vars, _value('unknown', line=stmt.lineno), state)
            return self._block(stmt.body, state)
        if isinstance(stmt, (ast.Try, getattr(ast, 'TryStar', ast.Try))):
            normal, _ = self._block(stmt.body, deepcopy(state))
            normal, _ = self._block(stmt.orelse, normal)
            branches = [state, normal]
            for handler in stmt.handlers:
                caught = deepcopy(state)
                if handler.name:
                    caught[handler.name] = _value('unknown', line=handler.lineno)
                handled, _ = self._block(handler.body, caught)
                branches.append(handled)
            result = branches[0]
            for other in branches[1:]:
                result = _merge(result, other)
            return self._block(stmt.finalbody, result)
        if isinstance(stmt, (ast.Import, ast.ImportFrom)):
            # Imports can rebind a parameter; never retain a stale type hint.
            # Actual import resolution remains in the import resolver.
            for alias in stmt.names:
                bound = alias.asname or (alias.name.split('.')[0] if isinstance(stmt, ast.Import)
                                         else alias.name)
                # Previously unseen imports belong to the regular import
                # resolver, and must not suppress legitimate imported calls.
                if bound in state:
                    state[bound] = _value('unknown', line=stmt.lineno)
            return state, True
        if isinstance(stmt, ast.Delete):
            # `del ctx` invalidates an earlier annotation or constructor.
            for target in stmt.targets:
                self._assign(target, _value('unknown', line=stmt.lineno), state)
            return state, True
        if isinstance(stmt, ast.Return):
            self._expr(stmt.value, state)
            return state, False
        if isinstance(stmt, ast.Raise):
            self._expr(stmt.exc, state)
            return state, False
        if isinstance(stmt, (ast.Break, ast.Continue)):
            return state, False
        if isinstance(stmt, ast.Expr):
            self._expr(stmt.value, state)
            return state, True
        # Fallback: inspect nested expression calls; invalidate names written
        # by unsupported constructs to avoid relying on stale bindings.
        for child in ast.iter_child_nodes(stmt):
            if isinstance(child, ast.expr):
                self._expr(child, state)
        return state, True

    def _block(self, statements: list[ast.stmt], state: dict[str, dict]) -> tuple[dict[str, dict], bool]:
        for stmt in statements:
            state, reachable = self._statement(stmt, state)
            if not reachable:
                return state, False
        return state, True

    def analyze(self, tree: ast.AST) -> dict[tuple[int, int], dict]:
        self._block(getattr(tree, 'body', []), {})
        return self.snapshots
