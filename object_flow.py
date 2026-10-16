"""Bounded, context-sensitive abstract execution for internal object flow.

No target code is executed. Values are finite sets of references/allocation
sites plus explicit unknown/None alternatives. Recursive cutoffs yield top,
never a guessed class. The legacy graph resolver remains the fallback for
syntax outside this interpreter's supported subset.
"""
from __future__ import annotations

import ast
from dataclasses import dataclass, replace


# ast.TryStar was introduced in Python 3.11; KnitCode supports Python 3.10+.
TRY_STATEMENTS = (ast.Try,) + ((ast.TryStar,) if hasattr(ast, 'TryStar') else ())


@dataclass(frozen=True)
class Atom:
    kind: str
    name: str = ''
    site: str = ''
    receiver_class: str = ''  # For a bound method: the allocation site's class.


@dataclass(frozen=True)
class Value:
    atoms: frozenset[Atom] = frozenset()
    tags: frozenset[str] = frozenset()

    def join(self, other):
        atoms = self.atoms | other.atoms
        return Value(atoms if len(atoms) <= 32 else frozenset({Atom('unknown')}), self.tags | other.tags)

    def tagged(self, tag):
        return replace(self, tags=self.tags | {tag})


UNKNOWN = Value(frozenset({Atom('unknown')}))
NONE = Value(frozenset({Atom('none')}))
BOTTOM = Value()


def one(kind, name='', site=''):
    return Value(frozenset({Atom(kind, name, site)}))


def merge_maps(a, b):
    return {k: a.get(k, UNKNOWN).join(b.get(k, UNKNOWN)) for k in a.keys() | b.keys()}


class ObjectFlow:
    def __init__(self, resolver, *, fields=False, dispatch=False):
        self.r = resolver
        self.fields = fields
        self.dispatch = dispatch
        self.functions = {}
        self.modules = {}
        self.events = {}
        self.summaries = {}
        self.stack = []
        self.return_heaps = []
        self.steps = 0                  # Total work, for diagnostics only.
        self.root_steps = 0             # Budget belongs to one entry context.
        self.root_step_limit = 6000
        self.cutoffs = 0
        self.generic_context_fallbacks = 0
        self.specific = set()
        self.call_index = {(c['file'], c['line'], c['column'], c.get('end_line'), c.get('end_column')): c for c in resolver.calls}
        for file, source in resolver.data.get('analysis_sources', {}).items():
            tree = ast.parse(source)
            self.modules[file] = tree
            index = {n.lineno: n for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
            for symbol in resolver.symbols:
                if symbol['file'] == file and symbol['kind'] in ('FUNCTION', 'METHOD') and symbol['line'] in index:
                    self.functions[symbol['id']] = index[symbol['line']]

    def refs(self, name, context, node):
        call = {**context, 'line': getattr(node, 'lineno', context.get('line', 0))}
        imports = self.r.imports_at(call)
        refs = self.r._resolve_reference(name, context['module'], imports)
        if not refs and '.' not in name:
            parts = context.get('scope', '').split('.')
            while parts and not refs:
                refs = self.r._lookup('.'.join([context['module'], *parts, name]))
                parts.pop()
        return refs

    def hint(self, node, context):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            try:
                node = ast.parse(node.value, mode='eval').body
            except SyntaxError:
                return UNKNOWN
        if not isinstance(node, (ast.Name, ast.Attribute)):
            return UNKNOWN
        refs = self.refs(ast.unparse(node), context, node)
        if len(refs) == 1 and refs[0]['kind'] == 'CLASS':
            return one('object', refs[0]['canonical_name'], 'hint:' + refs[0]['id']).join(UNKNOWN).tagged('annotation')
        return UNKNOWN

    def note(self, node, context, mode, targets, managed, tags=()):
        call = self.call_index.get((context['file'], node.lineno, node.col_offset, node.end_lineno, node.end_col_offset))
        if call is None:
            return
        key = (call['id'], mode)
        item = self.events.setdefault(key, {'targets': set(), 'managed': False, 'trace': set(), 'owner': call.get('caller_id')})
        item['targets'].update(s['id'] for s in targets)
        item['managed'] |= managed
        item['trace'].update(tags)
        item['trace'].add('context:' + ' > '.join(self.stack[-4:]))

    def invoke(self, symbol, args, kwargs, heap, mode, site):
        if not self.stack:
            # Each independent method/function entry gets a fair bounded
            # opportunity, even if earlier files contain many expressions.
            self.root_steps = 0
        fn = self.functions.get(symbol['id'])
        if fn is None or symbol.get('decorators') and any(d not in ('classmethod', 'staticmethod') for d in symbol['decorators']):
            return UNKNOWN.tagged('return')
        if isinstance(fn, ast.AsyncFunctionDef) or self.is_generator(fn):
            return UNKNOWN.tagged('return')
        if symbol['id'] in self.stack or len(self.stack) >= 16 or self.root_steps >= self.root_step_limit:
            self.cutoffs += 1
            return UNKNOWN.tagged('return')
        if mode == 'specific':
            self.specific.add(symbol['id'])
        context = {**symbol, 'scope': symbol['qualified_name'], 'caller_id': symbol['id']}
        params = [*fn.args.posonlyargs, *fn.args.args]
        if mode == 'specific':
            required = len(params) - len(fn.args.defaults)
            known = {p.arg for p in [*params, *fn.args.kwonlyargs]}
            invalid = (len(args) > len(params) and fn.args.vararg is None)
            invalid |= any(p.arg in kwargs for p in params[:len(args)])
            invalid |= any(p.arg in kwargs for p in fn.args.posonlyargs)
            invalid |= any(i >= len(args) and p.arg not in kwargs for i, p in enumerate(params[:required]))
            invalid |= any(p.arg not in kwargs and default is None for p, default in zip(fn.args.kwonlyargs, fn.args.kw_defaults))
            invalid |= any(k not in known for k in kwargs) and fn.args.kwarg is None
            if invalid:
                return UNKNOWN.tagged('return')
        env = {}
        for i, param in enumerate(params):
            value = args[i] if i < len(args) else kwargs.get(param.arg, self.hint(param.annotation, context))
            env[param.arg] = value.tagged('context') if mode == 'specific' else value
        for param in fn.args.kwonlyargs:
            env[param.arg] = kwargs.get(param.arg, self.hint(param.annotation, context))
        # Python determines locals lexically, even before their first store.
        for n in self.own_nodes(fn):
            if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Store):
                env.setdefault(n.id, UNKNOWN)
        self.stack.append(symbol['id'])
        self.return_heaps.append([])
        _, result, live = self.block(fn.body, env, heap, context, mode, site)
        returned_heaps = self.return_heaps.pop()
        self.stack.pop()
        if live:
            result = result.join(NONE)
            returned_heaps.append(dict(heap))
        if returned_heaps:
            merged_heap = returned_heaps[0]
            for branch_heap in returned_heaps[1:]:
                merged_heap = merge_maps(merged_heap, branch_heap)
            heap.clear(); heap.update(merged_heap)
        stub = all(isinstance(n, ast.Expr) and isinstance(n.value, ast.Constant)
                   and (n.value.value is Ellipsis or isinstance(n.value.value, str)) for n in fn.body)
        # Concrete bodies outrank advisory hints; hints only supplement top or
        # ellipsis stubs, never contradict a known return value or implicit None.
        if stub or any(a.kind == 'unknown' for a in result.atoms):
            result = (UNKNOWN if stub else result).join(self.hint(fn.returns, context))
        result = result.tagged('return:' + symbol['canonical_name']).tagged('return')
        self.summaries[symbol['id']] = self.summaries.get(symbol['id'], BOTTOM).join(result)
        return result

    @staticmethod
    def own_nodes(fn):
        pending = list(fn.body)
        while pending:
            node = pending.pop()
            yield node
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)):
                pending.extend(ast.iter_child_nodes(node))

    def is_generator(self, fn):
        return any(isinstance(n, (ast.Yield, ast.YieldFrom)) for n in self.own_nodes(fn))

    def expr(self, node, env, heap, context, mode, site):
        if node is None:
            return NONE
        self.steps += 1
        self.root_steps += 1
        if self.root_steps > self.root_step_limit:
            return UNKNOWN
        if isinstance(node, ast.Name):
            if node.id in env:
                return env[node.id]
            refs = self.refs(node.id, context, node)
            return Value(frozenset(Atom('reference', s['id']) for s in refs)) if refs else UNKNOWN
        if isinstance(node, ast.Constant):
            return NONE if node.value is None else one('literal', repr(node.value))
        if isinstance(node, (ast.List, ast.Tuple, ast.Set)):
            # Keep collection elements distinct from directly callable values.
            # Only an explicit for-loop may unwrap the finite element set.
            elements = BOTTOM
            for item in node.elts:
                values = self.expr(item, env, heap, context, mode, site)
                elements = elements.join(Value(frozenset(
                    Atom('collection_' + atom.kind, atom.name, atom.site, atom.receiver_class)
                    for atom in values.atoms), values.tags))
            return elements if node.elts else one('collection_unknown')
        if isinstance(node, ast.Lambda):
            # A lambda expression only evaluates its default arguments now;
            # its body is deferred until the lambda object is invoked.
            for default in [*node.args.defaults, *node.args.kw_defaults]:
                self.expr(default, env, heap, context, mode, site)
            return UNKNOWN
        if isinstance(node, ast.IfExp):
            self.expr(node.test, env, heap, context, mode, site)
            left, right = dict(heap), dict(heap)
            a = self.expr(node.body, dict(env), left, context, mode, site)
            b = self.expr(node.orelse, dict(env), right, context, mode, site)
            heap.clear(); heap.update(merge_maps(left, right))
            return a.join(b)
        if isinstance(node, ast.Attribute):
            receiver = self.expr(node.value, env, heap, context, mode, site)
            result = BOTTOM
            for atom in receiver.atoms:
                if atom.kind != 'object':
                    result = result.join(UNKNOWN)
                    continue
                # Instance attributes take priority over non-data methods.
                field = heap.get((atom.site, node.attr)) if self.fields else None
                if field is not None:
                    result = result.join(field.tagged('field'))
                    continue
                cls = self.r.classes.get(atom.name)
                methods, _ = self.r._find_method(cls, node.attr) if cls else ([], [])
                if methods:
                    # Store the receiver as part of the value.  A later fn()
                    # call must use the object that produced fn = obj.method.
                    bound_methods = frozenset(
                        Atom('bound_method', fn['id'], atom.site, atom.name)
                        for fn in methods if all(
                            decorator in ('classmethod', 'staticmethod')
                            for decorator in fn.get('decorators', [])))
                    # A property or an arbitrary decorated descriptor may
                    # yield a non-callable value rather than the method.
                    result = result.join(Value(bound_methods) if len(bound_methods) == len(methods) else UNKNOWN)
                else:
                    result = result.join(UNKNOWN)
            # Module/class qualified references are resolved only if the root
            # is not an actual local receiver (no name-based fallback).
            root = node
            while isinstance(root, ast.Attribute):
                root = root.value
            if isinstance(root, ast.Name) and root.id not in env:
                refs = self.refs(ast.unparse(node), context, node)
                if refs:
                    return Value(frozenset(Atom('reference', s['id']) for s in refs))
            tracked = any(a.kind == 'object' for a in receiver.atoms) or bool(receiver.tags & {'return', 'field'})
            return result.tagged('field') if self.fields and tracked else result
        if isinstance(node, ast.Call):
            return self.call(node, env, heap, context, mode, site)
        for child in ast.iter_child_nodes(node):
            if isinstance(child, ast.expr):
                self.expr(child, env, heap, context, mode, site)
        return UNKNOWN

    def call(self, node, env, heap, context, mode, site):
        receiver = None
        targets = []
        bindings = []
        managed = False
        tags = set()
        if isinstance(node.func, ast.Attribute):
            if (isinstance(node.func.value, ast.Call) and isinstance(node.func.value.func, ast.Name)
                    and node.func.value.func.id == 'super'):
                proxy = node.func.value
                owner = self.r._owner_class(context)
                symbol = self.r.by_id.get(context.get('caller_id'), {})
                params = symbol.get('parameters', [])
                call = self.call_index.get((context['file'], node.lineno, node.col_offset, node.end_lineno, node.end_col_offset))
                allowed = False
                if call:
                    _, why, _ = self.r._resolve_super(call, owner, self.r.imports_at(call))
                    allowed = why in ('super_c3_may_call', 'super_method_not_found')
                receiver = env.get(params[0], UNKNOWN) if params else UNKNOWN
                anchor = owner
                if len(proxy.args) == 2:
                    refs = self.refs(ast.unparse(proxy.args[0]), context, node)
                    anchor = refs[0] if len(refs) == 1 and refs[0]['kind'] == 'CLASS' else None
                if allowed and anchor:
                    for atom in receiver.atoms:
                        if atom.kind != 'object':
                            continue
                        order, status = self.r._mro(self.r.classes[atom.name])
                        if status == 'complete' and anchor['canonical_name'] in order:
                            found, path = self.r._lookup_mro(order[order.index(anchor['canonical_name']) + 1:], node.func.attr)
                            for fn in found:
                                targets.append(fn); bindings.append((fn, one('object', atom.name, atom.site)))
                            tags.update(path)
                managed = self.dispatch and mode == 'specific'
                tags.update(receiver.tags)
                tags.add('super_receiver_context')
            else:
                receiver = self.expr(node.func.value, env, heap, context, mode, site)
                tags.update(receiver.tags)
                managed = bool(receiver.tags & {'return', 'field'}) or isinstance(node.func.value, ast.Call)
                if self.dispatch and mode == 'specific' and 'context' in receiver.tags:
                    managed = True
                for atom in receiver.atoms:
                    if atom.kind == 'object':
                        cls = self.r.classes.get(atom.name)
                        # Instance attributes shadow non-data method descriptors.
                        if self.fields and (atom.site, node.func.attr) in heap:
                            managed = True
                            tags.add('field_shadow')
                            field_value = heap[(atom.site, node.func.attr)]
                            tags.update(field_value.tags)
                            for field_atom in field_value.atoms:
                                if field_atom.kind == 'bound_method' and field_atom.name in self.r.by_id:
                                    fn = self.r.by_id[field_atom.name]
                                    targets.append(fn)
                                    bound = one('object', field_atom.receiver_class, field_atom.site)
                                    if 'classmethod' in fn.get('decorators', []):
                                        owner = self.r.classes.get(field_atom.receiver_class)
                                        if owner:
                                            bound = one('reference', owner['id'])
                                    bindings.append((fn, bound))
                                elif field_atom.kind == 'reference' and field_atom.name in self.r.by_id:
                                    fn = self.r.by_id[field_atom.name]
                                    targets.append(fn); bindings.append((fn, None))
                                elif field_atom.kind == 'object' and not field_atom.site.startswith('hint:'):
                                    field_cls = self.r.classes.get(field_atom.name)
                                    methods, _ = self.r._find_method(field_cls, '__call__') if field_cls else ([], [])
                                    for fn in methods:
                                        targets.append(fn)
                                        bindings.append((fn, one('object', field_atom.name, field_atom.site)))
                            continue
                        found, _ = self.r._find_method(cls, node.func.attr) if cls else ([], [])
                        for fn in found:
                            targets.append(fn)
                            bindings.append((fn, one('object', atom.name, atom.site)))
                if not targets and not managed:
                    refs = self.refs(ast.unparse(node.func), context, node)
                    for ref in refs:
                        targets.append(ref); bindings.append((ref, None))
        else:
            function = self.expr(node.func, env, heap, context, mode, site)
            tags.update(function.tags)
            for atom in function.atoms:
                if atom.kind == 'reference' and atom.name in self.r.by_id:
                    fn = self.r.by_id[atom.name]
                    targets.append(fn); bindings.append((fn, None))
                elif atom.kind == 'bound_method' and atom.name in self.r.by_id:
                    fn = self.r.by_id[atom.name]
                    bound = one('object', atom.receiver_class, atom.site)
                    if 'classmethod' in fn.get('decorators', []):
                        owner = self.r.classes.get(atom.receiver_class)
                        if owner:
                            bound = one('reference', owner['id'])
                    targets.append(fn); bindings.append((fn, bound))
                    managed = True  # A bound method is a tracked callable value.
                    tags.add('bound_method_alias')
                elif atom.kind == 'object' and not atom.site.startswith('hint:'):
                    # Python uses type(obj).__call__ for obj(); instance
                    # attributes named __call__ cannot override that lookup.
                    cls = self.r.classes.get(atom.name)
                    methods, _ = self.r._find_method(cls, '__call__') if cls else ([], [])
                    for fn in methods:
                        targets.append(fn)
                        bindings.append((fn, one('object', atom.name, atom.site)))
                    managed = True
                    tags.add('callable_instance')
        args = [self.expr(a, env, heap, context, mode, site) for a in node.args]
        kwargs = {k.arg: self.expr(k.value, env, heap, context, mode, site) for k in node.keywords}
        if receiver:
            for atom in receiver.atoms:
                if atom.kind == 'object':
                    tags.update(('receiver:' + atom.name, 'allocation:' + atom.site))
                else:
                    tags.add('receiver_alternative:' + atom.kind)
        self.note(node, context, mode, targets, managed, tags)
        callsite = f"{context['file']}:{node.lineno}:{node.col_offset}"
        child_site = callsite + ' > ' + site
        child_mode = 'generic' if mode == 'generic' else 'specific'
        if any(isinstance(arg, ast.Starred) for arg in node.args) or None in kwargs:
            return UNKNOWN.tagged('return')
        result = BOTTOM
        start_heap = dict(heap)
        outcomes = []
        for fn, bound in bindings:
            branch_heap = dict(start_heap)
            if fn['kind'] == 'CLASS':
                custom_new, _ = self.r._find_method(fn, '__new__')
                if self.r._mro(fn)[1].startswith('invalid') or custom_new:
                    value = UNKNOWN.tagged('return')
                else:
                    allocation = child_site
                    value = one('object', fn['canonical_name'], allocation)
                    if self.fields:
                        initializers, _ = self.r._find_method(fn, '__init__')
                        for init in initializers:
                            self.invoke(init, [value, *args], kwargs, branch_heap, child_mode, child_site)
            else:
                actual = args
                if bound is not None and 'staticmethod' not in fn.get('decorators', []):
                    actual = [bound, *args]
                value = self.invoke(fn, actual, kwargs, branch_heap, child_mode, child_site)
            result = result.join(value)
            outcomes.append(branch_heap)
        if outcomes:
            merged = outcomes[0]
            for branch in outcomes[1:]:
                merged = merge_maps(merged, branch)
            heap.clear(); heap.update(merged)
        else:
            result = UNKNOWN.tagged('return')
            if self.fields:
                # Unknown callees may mutate escaping objects. Invalidate
                # reachable fields instead of keeping stale strong updates.
                escaped = {a.site for v in [*args, *kwargs.values(), receiver or BOTTOM]
                           for a in v.atoms if a.kind == 'object'}
                while True:
                    more = {a.site for (obj, _), v in heap.items() if obj in escaped
                            for a in v.atoms if a.kind == 'object'} - escaped
                    if not more:
                        break
                    escaped.update(more)
                for key in heap:
                    if key[0] in escaped:
                        heap[key] = UNKNOWN.tagged('field')
        return result

    def assign(self, target, value, env, heap, context, mode, site):
        if isinstance(target, ast.Name):
            env[target.id] = value
        elif isinstance(target, ast.Attribute) and self.fields:
            receiver = self.expr(target.value, env, heap, context, mode, site)
            objects = [a for a in receiver.atoms if a.kind == 'object']
            strong = len(receiver.atoms) == 1 and len(objects) == 1 and not objects[0].site.startswith('hint:')
            for obj in objects:
                key = (obj.site, target.attr)
                heap[key] = value if strong else heap.get(key, UNKNOWN).join(value)
        elif isinstance(target, (ast.Tuple, ast.List)):
            for part in target.elts:
                self.assign(part, UNKNOWN, env, heap, context, mode, site)

    def block(self, body, env, heap, context, mode, site):
        returns = BOTTOM
        for stmt in body:
            if self.root_steps > self.root_step_limit:
                self.cutoffs += 1
                return env, returns.join(UNKNOWN), True
            if isinstance(stmt, ast.Return):
                value = self.expr(stmt.value, env, heap, context, mode, site)
                if self.return_heaps:
                    self.return_heaps[-1].append(dict(heap))
                return env, returns.join(value), False
            if isinstance(stmt, ast.Raise):
                # Calls in the exception expression execute before the raise.
                self.expr(stmt.exc, env, heap, context, mode, site)
                self.expr(stmt.cause, env, heap, context, mode, site)
                return env, returns, False
            if isinstance(stmt, (ast.Break, ast.Continue)):
                # End the current approximated loop iteration.  The enclosing
                # loop retains a path that leaves the loop afterwards.
                return env, returns, True
            if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                continue
            if isinstance(stmt, (ast.Pass, ast.Import, ast.ImportFrom)):
                continue
            if isinstance(stmt, (ast.Assign, ast.AnnAssign)):
                value = self.expr(stmt.value, env, heap, context, mode, site)
                for target in stmt.targets if isinstance(stmt, ast.Assign) else [stmt.target]:
                    self.assign(target, value, env, heap, context, mode, site)
            elif isinstance(stmt, ast.Expr):
                self.expr(stmt.value, env, heap, context, mode, site)
            elif isinstance(stmt, ast.AugAssign):
                self.expr(stmt.value, env, heap, context, mode, site)
                self.assign(stmt.target, UNKNOWN, env, heap, context, mode, site)
            elif isinstance(stmt, ast.Delete):
                for target in stmt.targets:
                    self.assign(target, UNKNOWN, env, heap, context, mode, site)
            elif isinstance(stmt, ast.Assert):
                self.expr(stmt.test, env, heap, context, mode, site)
                self.expr(stmt.msg, env, heap, context, mode, site)
            elif isinstance(stmt, (ast.With, ast.AsyncWith)):
                # __enter__ can raise or return an arbitrary object.  No
                # assumptions about the context manager's yielded type.
                with_env, with_heap = dict(env), dict(heap)
                for item in stmt.items:
                    self.expr(item.context_expr, with_env, with_heap, context, mode, site)
                    if item.optional_vars is not None:
                        self.assign(item.optional_vars, UNKNOWN, with_env, with_heap, context, mode, site)
                with_env, ret, live = self.block(stmt.body, with_env, with_heap, context, mode, site)
                returns = returns.join(ret)
                # Context manager entry may fail before the body starts.
                env = merge_maps(env, with_env) if live else env
                heap.clear(); heap.update(merge_maps(heap, with_heap))
                if not live:
                    return env, returns, False
            elif isinstance(stmt, (ast.For, ast.AsyncFor, ast.While)):
                if (isinstance(stmt, (ast.For, ast.AsyncFor))
                        and isinstance(stmt.iter, (ast.List, ast.Tuple, ast.Set))
                        and not stmt.iter.elts):
                    env, ret, live = self.block(stmt.orelse, env, heap, context, mode, site)
                    returns = returns.join(ret)
                    if not live:
                        return env, returns, False
                    continue
                if (isinstance(stmt, ast.While) and isinstance(stmt.test, ast.Constant)
                        and not stmt.test.value):
                    env, ret, live = self.block(stmt.orelse, env, heap, context, mode, site)
                    returns = returns.join(ret)
                    if not live:
                        return env, returns, False
                    continue
                # Zero-or-one iteration approximation, with explicit joining.
                # The zero path prevents a loop-body assignment from being
                # treated as guaranteed.  This is not a fixed-point solver.
                loop_env, loop_heap = dict(env), dict(heap)
                if isinstance(stmt, (ast.For, ast.AsyncFor)):
                    iterable = stmt.iter
                    element = UNKNOWN
                    collection = self.expr(iterable, env, heap, context, mode, site)
                    unwrapped = frozenset(
                        Atom(atom.kind.removeprefix('collection_'), atom.name, atom.site, atom.receiver_class)
                        for atom in collection.atoms if atom.kind.startswith('collection_')
                        and atom.kind != 'collection_unknown')
                    # Arbitrary iterables may yield values we cannot model.
                    other = any(not atom.kind.startswith('collection_') or atom.kind == 'collection_unknown'
                                for atom in collection.atoms)
                    element = Value(unwrapped).join(UNKNOWN) if other else Value(unwrapped)
                    if not element.atoms:
                        element = UNKNOWN
                    self.assign(stmt.target, element, loop_env, loop_heap, context, mode, site)
                else:
                    self.expr(stmt.test, env, heap, context, mode, site)
                loop_env, loop_ret, loop_live = self.block(stmt.body, loop_env, loop_heap, context, mode, site)
                returns = returns.join(loop_ret)
                # Loop else executes on normal exhaustion, not necessarily on
                # break.  An alternative zero-iteration path is always safe.
                else_env, else_heap = dict(env), dict(heap)
                else_env, else_ret, else_live = self.block(stmt.orelse, else_env, else_heap, context, mode, site)
                returns = returns.join(else_ret)
                alive = [(else_env, else_heap)] if else_live else []
                if loop_live:
                    alive.append((loop_env, loop_heap))
                if not alive:
                    return env, returns, False
                env, merged_heap = alive[0]
                for e, h in alive[1:]:
                    env = merge_maps(env, e)
                    merged_heap = merge_maps(merged_heap, h)
                heap.clear(); heap.update(merged_heap)
            elif isinstance(stmt, TRY_STATEMENTS):
                entry_env, entry_heap = dict(env), dict(heap)
                normal_env, normal_heap = dict(env), dict(heap)
                normal_env, normal_ret, normal_live = self.block(stmt.body, normal_env, normal_heap, context, mode, site)
                returns = returns.join(normal_ret)
                alive = []
                if normal_live:
                    normal_env, else_ret, normal_live = self.block(stmt.orelse, normal_env, normal_heap, context, mode, site)
                    returns = returns.join(else_ret)
                    if normal_live:
                        alive.append((normal_env, normal_heap))
                # An exception may occur at any prefix of the try suite.
                # Join entry/finished states before starting each handler.
                for handler in stmt.handlers:
                    handler_env = merge_maps(entry_env, normal_env)
                    handler_heap = merge_maps(entry_heap, normal_heap)
                    self.expr(handler.type, handler_env, handler_heap, context, mode, site)
                    if handler.name:
                        handler_env[handler.name] = UNKNOWN
                    handler_env, ret, handler_live = self.block(handler.body, handler_env, handler_heap, context, mode, site)
                    returns = returns.join(ret)
                    if handler_live:
                        alive.append((handler_env, handler_heap))
                # Unhandled exceptions do not fall through to the next
                # statement in the current function.
                had_normal_exit = bool(alive)
                if stmt.finalbody:
                    # finally also executes during exception unwinding or a
                    # return.  Include that path without turning its effects
                    # into a guaranteed result on every normal path.
                    if not alive:
                        alive.append((merge_maps(entry_env, normal_env), merge_maps(entry_heap, normal_heap)))
                    finalized = []
                    for branch_env, branch_heap in alive:
                        branch_env, ret, live = self.block(stmt.finalbody, branch_env, branch_heap, context, mode, site)
                        returns = returns.join(ret)
                        if live:
                            finalized.append((branch_env, branch_heap))
                    alive = finalized
                    if not had_normal_exit:
                        # finally runs even during return/unwinding, but it
                        # cannot make that pending return fall through.
                        return env, returns, False
                if not alive:
                    return env, returns, False
                env, merged_heap = alive[0]
                for e, h in alive[1:]:
                    env = merge_maps(env, e)
                    merged_heap = merge_maps(merged_heap, h)
                heap.clear(); heap.update(merged_heap)
            elif isinstance(stmt, ast.If):
                self.expr(stmt.test, env, heap, context, mode, site)
                branches = [stmt.body, stmt.orelse]
                if isinstance(stmt.test, ast.Constant):
                    branches = [stmt.body if stmt.test.value else stmt.orelse]
                live_envs, live_heaps, all_heaps = [], [], []
                for branch in branches:
                    branch_heap = dict(heap)
                    branch_env, ret, live = self.block(branch, dict(env), branch_heap, context, mode, site)
                    returns = returns.join(ret)
                    all_heaps.append(branch_heap)
                    if live:
                        live_envs.append(branch_env); live_heaps.append(branch_heap)
                if not live_envs:
                    merged_heap = all_heaps[0]
                    for branch_heap in all_heaps[1:]:
                        merged_heap = merge_maps(merged_heap, branch_heap)
                    heap.clear(); heap.update(merged_heap)
                    return env, returns, False
                env = live_envs[0]
                merged_heap = live_heaps[0]
                for branch_env, branch_heap in zip(live_envs[1:], live_heaps[1:]):
                    env = merge_maps(env, branch_env); merged_heap = merge_maps(merged_heap, branch_heap)
                heap.clear(); heap.update(merged_heap)
            else:
                # Unsupported compound statements: retain uncertainty, never
                # treat a syntactic return inside them as unconditional.
                for n in ast.walk(stmt):
                    if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Store):
                        env[n.id] = UNKNOWN
                    if isinstance(n, ast.Attribute) and isinstance(n.ctx, (ast.Store, ast.Del)):
                        for key in heap:
                            if key[1] == n.attr:
                                heap[key] = UNKNOWN.tagged('field')
                returns = returns.join(UNKNOWN)
        return env, returns, True

    def run(self):
        if self.fields:
            # Library methods are analyzed in a hypothetical owner-instance
            # context. Observed call contexts below take precedence over this
            # fallback; no subclass is invented merely from class membership.
            for symbol in self.r.symbols:
                if symbol['kind'] != 'METHOD' or symbol['name'] == '__init__':
                    continue
                cls = self.r.classes.get(symbol['module'] + '.' + symbol['scope'])
                if not cls or symbol.get('decorators'):
                    continue
                obj = one('object', cls['canonical_name'], 'root:' + symbol['id'])
                heap = {}
                inits, _ = self.r._find_method(cls, '__init__')
                for init in inits:
                    self.invoke(init, [obj], {}, heap, 'generic', 'root:' + symbol['id'])
                self.invoke(symbol, [obj], {}, heap, 'generic', 'root:' + symbol['id'])
        for symbol in self.r.symbols:
            if symbol['kind'] == 'FUNCTION' and symbol['id'] in self.functions:
                self.invoke(symbol, [], {}, {}, 'root', 'root:' + symbol['id'])
        # Module-level statements are real entry points too.
        for file, tree in self.modules.items():
            self.root_steps = 0
            module = next((s['module'] for s in self.r.symbols if s['file'] == file), '')
            self.block(tree.body, {}, {}, {'file': file, 'module': module, 'scope': '<module>'}, 'root', 'module:' + file)
        result = {}
        for call in self.r.calls:
            owner = call.get('caller_id')
            modes = ['specific'] if owner in self.specific else ['root', 'generic']
            chosen = [self.events[(call['id'], mode)] for mode in modes if (call['id'], mode) in self.events]
            # A caller-specific observation that only contains an unknown
            # receiver is not a proof that generic owner contexts are
            # impossible. Preserve those generic *may*-call candidates.
            # However a definite instance-field shadow is authoritative and
            # must not be overridden by a generic method fallback.
            if (modes == ['specific'] and chosen and not any(e['targets'] for e in chosen)
                    and not any('field_shadow' in e['trace'] for e in chosen)):
                fallback = [self.events[(call['id'], m)] for m in ('root', 'generic')
                            if (call['id'], m) in self.events
                            and self.events[(call['id'], m)]['targets']]
                if fallback:
                    chosen = fallback
                    self.generic_context_fallbacks += 1
            if any(e['managed'] for e in chosen):
                ids = set().union(*(e['targets'] for e in chosen))
                trace = sorted(set().union(*(e['trace'] for e in chosen)))
                result[call['id']] = ([self.r.by_id[i] for i in sorted(ids)], 'object_flow_may_call', trace)
        return result
