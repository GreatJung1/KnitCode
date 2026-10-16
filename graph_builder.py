"""Conservative scope-aware, evidence-rich Python call graph resolver.

May-call edges are generated for finite class candidates from a for-loop.
No cross-file name-only guessing: external/unknown calls remain unresolved.
"""
from __future__ import annotations
import ast
from collections import defaultdict
from import_resolution import canonical_import, ImportOrigins
from typing import Any


def build_graph(data: dict[str, Any]) -> dict[str, Any]:
    resolver = Resolver(data)
    return resolver.build()


class Resolver:
    def __init__(self, data: dict[str, Any]):
        self.data = data
        self.symbols = data.get('symbols', [])
        self.calls = data.get('calls', [])
        self.origins = ImportOrigins(data)
        self.by_canonical = defaultdict(list)
        self.by_id = {}
        self.classes = {}
        self.assignments = defaultdict(list)
        self.imports = defaultdict(list)
        self._mro_cache = {}
        self._generic_check_active = set()
        self._generic_getitem_cache = {}
        for s in self.symbols:
            self.by_canonical[s['canonical_name']].append(s)
            self.by_id[s['id']] = s
            if s['kind'] == 'CLASS':
                self.classes[s['canonical_name']] = s
        for ass in data.get('assignments', []):
            self.assignments[(ass['file'], ass['scope'])].append(ass)
        for key in self.assignments:
            self.assignments[key].sort(key=lambda a: (a.get('line') or 0, a.get('column') or 0))
        for imp in data.get('imports', []):
            self.imports[imp['file']].append(imp)

    @staticmethod
    def _scope_visible(inner: str, outer: str) -> bool:
        return outer == '<module>' or inner == outer or inner.startswith(outer + '.')

    def imports_at(self, call: dict) -> dict[str, str]:
        imports = {}
        for imp in self.imports[call['file']]:
            scope = imp.get('scope') or '<module>'
            if not self._scope_visible(call['scope'], scope):
                continue
            if scope != '<module>' and (imp.get('line') or 0) > (call.get('line') or 0):
                continue
            target = canonical_import(imp)
            if imp.get('local_name'):
                imports[imp['local_name']] = target
                # A later same-module function/class definition replaces an
                # imported binding (normal module initialization semantics).
                candidate = self._lookup(imp['module'] + '.' + imp['local_name'])
                if (scope == '<module>' and candidate and
                    max(s.get('line') or 0 for s in candidate) > (imp.get('line') or 0)):
                    imports.pop(imp['local_name'], None)
        return imports

    def _lookup(self, canonical: str) -> list[dict]:
        return self.by_canonical.get(canonical, [])

    def _owner_class(self, call: dict) -> dict | None:
        parts = (call.get('scope') or '').split('.')
        for i in range(len(parts) - 1, 0, -1):
            candidate = '.'.join([call['module'], *parts[:i]])
            if candidate in self.classes:
                return self.classes[candidate]
        return None

    # These two CPython stdlib base classes have a known, fixed hierarchy.
    # Treat them as MRO-only nodes, never as source-backed callable targets.
    _KNOWN_STDLIB_BASES = frozenset({'typing.Generic', 'abc.ABC'})

    def _delegates_generic_getitem(self, symbol: dict) -> bool:
        """Admit a custom __class_getitem__ only when every return is super().

        For example, Click ParamType fills in omitted type parameters and then
        delegates to typing.Generic.__class_getitem__. User-written methods
        returning unrelated classes/aliases must stay opaque.
        """
        key = (symbol['file'], symbol.get('line'))
        if key in self._generic_getitem_cache:
            return self._generic_getitem_cache[key]
        result = False
        source = (self.data.get('analysis_sources') or {}).get(symbol['file'])
        if source:
            try:
                tree = ast.parse(source)
                matches = [n for n in ast.walk(tree)
                           if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
                           and n.name == '__class_getitem__' and n.lineno == symbol.get('line')]
                if len(matches) == 1:
                    fn = matches[0]
                    # Do not certify a function if super is rebound locally.
                    shadows_super = any(isinstance(n, (ast.Name, ast.arg)) and
                                        (n.id if isinstance(n, ast.Name) else n.arg) == 'super' and
                                        (isinstance(n, ast.arg) or isinstance(n.ctx, (ast.Store, ast.Del)))
                                        for n in ast.walk(fn))
                    returns = [n for n in ast.walk(fn) if isinstance(n, ast.Return)]
                    if returns and not shadows_super:
                        def delegated(node):
                            value = node.value
                            return (isinstance(value, ast.Call)
                                    and isinstance(value.func, ast.Attribute)
                                    and value.func.attr == '__class_getitem__'
                                    and isinstance(value.func.value, ast.Call)
                                    and isinstance(value.func.value.func, ast.Name)
                                    and value.func.value.func.id == 'super'
                                    and not value.func.value.args
                                    and not value.func.value.keywords)
                        result = all(delegated(node) for node in returns)
            except (SyntaxError, ValueError):
                pass
        self._generic_getitem_cache[key] = result
        return result

    def _generic_alias_is_class(self, cls: dict) -> bool:
        """Require source-backed Generic ancestry and no opaque subscription override."""
        key = cls['canonical_name']
        if key in self._generic_check_active:
            return False
        self._generic_check_active.add(key)
        try:
            order, status = self._mro(cls)
            if status != 'complete' or 'typing.Generic' not in order:
                return False
            for canonical in order:
                if canonical not in self.classes:
                    continue
                parent = self.classes[canonical]
                if any('__class_getitem__' in ass.get('targets', [])
                       for ass in self.assignments.get((parent['file'], parent['qualified_name']), [])):
                    return False
                for method in self._lookup(canonical + '.__class_getitem__'):
                    if method['kind'] != 'METHOD' or not self._delegates_generic_getitem(method):
                        return False
            return True
        finally:
            self._generic_check_active.remove(key)

    @staticmethod
    def _safe_union_operand(expr: ast.AST, imports: dict[str, str], context: dict,
                            binding_check) -> bool:
        """Only trusted builtin/stdlib type operands can form an eager | union."""
        if isinstance(expr, ast.Name):
            return (expr.id in {'str', 'bytes', 'int', 'bool', 'float', 'object'}
                    and expr.id not in imports and not binding_check(context, expr.id))
        if isinstance(expr, ast.Subscript):
            return (Resolver._safe_union_operand(expr.value, imports, context, binding_check) and
                    Resolver._static_type_argument(expr.slice, imports, context, binding_check))
        if isinstance(expr, ast.Attribute):
            name = ast.unparse(expr)
            root = name.split('.')[0]
            canonical = imports.get(root, '') + name[len(root):]
            return (canonical in {'os.PathLike'} and not binding_check(context, root)
                    and not binding_check(context, name))
        return False

    @staticmethod
    def _static_type_argument(expr: ast.AST, imports: dict[str, str], context: dict,
                              binding_check) -> bool:
        """Reject effectful type arguments; allow a narrow, provenance-checked union."""
        if isinstance(expr, ast.BinOp) and isinstance(expr.op, ast.BitOr):
            return (Resolver._safe_union_operand(expr.left, imports, context, binding_check) and
                    Resolver._safe_union_operand(expr.right, imports, context, binding_check))
        if isinstance(expr, (ast.Name, ast.Constant)):
            return True
        if isinstance(expr, ast.Attribute):
            return Resolver._static_type_argument(expr.value, imports, context, binding_check)
        if isinstance(expr, (ast.Tuple, ast.List)):
            return all(Resolver._static_type_argument(x, imports, context, binding_check) for x in expr.elts)
        if isinstance(expr, ast.Subscript):
            return (Resolver._static_type_argument(expr.value, imports, context, binding_check) and
                    Resolver._static_type_argument(expr.slice, imports, context, binding_check))
        return False

    def _bare_base_name(self, expr: ast.AST, imports: dict[str, str], context: dict) -> str | None:
        """Only plain name/attribute bases and safe type subscripts are admitted."""
        if isinstance(expr, ast.Subscript):
            if not self._static_type_argument(expr.slice, imports, context, self._find_binding):
                return None
            expr = expr.value
        if isinstance(expr, (ast.Name, ast.Attribute)):
            return ast.unparse(expr)
        return None

    def _trusted_stdlib_import(self, cls: dict, root: str, origin: str) -> bool:
        """No conditional/competing import may masquerade as a known stub."""
        definitions = [s for s in self.symbols
                       if s['file'] == cls['file'] and s['module'] == cls['module']
                       and s.get('scope') == '<module>' and s['name'] == root
                       and (s.get('line') or 0) < (cls.get('line') or 0)]
        if definitions:
            return False
        options = [imp for imp in self.imports[cls['file']]
                   if imp.get('local_name') == root
                   and (imp.get('line') or 0) < (cls.get('line') or 0)
                   and imp.get('scope') == '<module>']
        return (len(options) == 1 and not options[0].get('control_depth')
                and canonical_import(options[0]) == origin)

    def _base_order(self, cls: dict) -> tuple[list[str], bool]:
        """Keep declaration order and uncertainty; never drop an opaque base."""
        imports = self.imports_at({"file": cls['file'], "module": cls['module'], "scope": cls['scope'] or '<module>', "line": cls.get('line') or 0})
        # Base expressions execute at class definition time, unlike a method
        # body which can observe imports performed later during initialization.
        for imp in self.imports[cls['file']]:
            if (imp.get('line') or 0) >= (cls.get('line') or 0):
                imports.pop(imp.get('local_name'), None)
        found, complete = [], True
        for base in cls.get('bases', []):
            try:
                expr = ast.parse(base or '', mode='eval').body
            except SyntaxError:
                complete = False
                continue
            context = {**cls, 'scope': cls.get('scope') or '<module>'}
            base_name = self._bare_base_name(expr, imports, context)
            if not base_name:
                complete = False
                continue
            is_subscript = isinstance(expr, ast.Subscript)
            # A subscription such as Base[T] is not the same thing as a base
            # class unless its result is known to preserve the class origin.
            root = base_name.split('.')[0]
            if (self._find_binding(context, root) or
                    (base_name != root and self._find_binding(context, base_name))):
                complete = False
                continue
            imported_name = imports.get(root)
            canonical_stdlib = ('.'.join([imported_name, *base_name.split('.')[1:]])
                                if imported_name else None)
            if canonical_stdlib in self._KNOWN_STDLIB_BASES:
                if (canonical_stdlib.split('.')[0] in self.origins.modules or
                        not self._trusted_stdlib_import(cls, root, imported_name)):
                    complete = False
                    continue
                # Generic[T] resolves through typing.Generic.__class_getitem__;
                # abc.ABC is not subscriptable.
                if not is_subscript or canonical_stdlib == 'typing.Generic':
                    found.append(canonical_stdlib)
                else:
                    complete = False
                continue
            if is_subscript and canonical_stdlib:
                # Do not treat an arbitrary imported __class_getitem__ as an
                # identity-preserving generic alias (PEP 560 __mro_entries__).
                complete = False
                continue
            base_candidates = self._resolve_reference(base_name, cls['module'], imports)
            if not base_candidates and cls.get('scope') != '<module>':
                scope = (cls.get('scope') or '').split('.')
                while scope and not base_candidates:
                    base_candidates = self._lookup('.'.join([cls['module'], *scope, base_name]))
                    scope.pop()
            if len(base_candidates) == 1 and base_candidates[0]['kind'] == 'CLASS':
                candidate = base_candidates[0]
                if candidate['file'] == cls['file'] and candidate['line'] >= cls['line']:
                    complete = False
                elif is_subscript and not self._generic_alias_is_class(candidate):
                    complete = False
                else:
                    found.append(candidate['canonical_name'])
            elif not is_subscript and base_name == 'object' and root not in imports and not base_candidates:
                found.append('builtins.object')
            else:
                complete = False
        return found, complete

    def _get_bases(self, cls: dict, seen: set[str] | None = None) -> list[dict]:
        names, _ = self._base_order(cls)
        return [self.classes[n] for n in names if n in self.classes]

    def _mro(self, cls: dict, active: frozenset[str] = frozenset()) -> tuple[list[str], str]:
        """C3: L(C) = C + merge(L(B1), ..., L(Bn), [B1,...,Bn]).

        Unknown hierarchy and invalid hierarchy are distinct. Neither permits
        a DFS fallback. object participates in merging but is never a node.
        """
        key = cls['canonical_name']
        if key in active:
            return [], 'invalid_cycle'
        if key in self._mro_cache:
            return self._mro_cache[key]
        if cls.get('class_keywords') or cls.get('decorators'):
            result = ([], 'unknown_class_customization')
        else:
            bases, complete = self._base_order(cls)
            if not complete:
                result = ([], 'unknown_base')
            elif len(set(bases)) != len(bases):
                result = ([], 'invalid_duplicate_base')
            else:
                bases = bases or ['builtins.object']
                sequences, reason = [], 'complete'
                for base in bases:
                    order, status = ((['builtins.object'], 'complete') if base == 'builtins.object'
                                     else ([base, 'builtins.object'], 'complete')
                                     if base in self._KNOWN_STDLIB_BASES
                                     else self._mro(self.classes[base], active | {key}))
                    if status != 'complete':
                        reason = status
                        break
                    sequences.append(list(order))
                if reason != 'complete':
                    result = ([], reason)
                else:
                    sequences.append(list(bases))
                    order = [key]
                    while any(sequences):
                        sequences = [s for s in sequences if s]
                        head = next((s[0] for s in sequences
                                     if not any(s[0] in t[1:] for t in sequences)), None)
                        if head is None:
                            reason = 'invalid_c3_conflict'
                            break
                        order.append(head)
                        for seq in sequences:
                            if seq[0] == head:
                                seq.pop(0)
                    result = (order if reason == 'complete' else [], reason)
        self._mro_cache[key] = result
        return result

    def _local_method(self, cls: dict, name: str) -> tuple[list[dict], bool]:
        local = self._lookup(cls['canonical_name'] + '.' + name)
        assignments = [a for a in self.assignments.get((cls['file'], cls['qualified_name']), [])
                       if name in a.get('targets', [])]
        # Non-method class attributes and descriptors stop inheritance lookup.
        if assignments and (not local or max(a['line'] for a in assignments) >= max(s['line'] for s in local)):
            return [], True
        if local:
            methods = [s for s in local if s['kind'] == 'METHOD'
                       and not s.get('control_depth')
                       and all(d in ('staticmethod', 'classmethod') for d in s.get('decorators', []))]
            return methods, True
        return [], False

    def _lookup_mro(self, order: list[str], name: str) -> tuple[list[dict], list[str]]:
        path = []
        for key in order:
            path.append(key)
            if key == 'builtins.object':
                break
            if key in self._KNOWN_STDLIB_BASES:
                # Only their known MRO position is modeled; no internal calls
                # are emitted for methods defined by the stdlib classes.
                continue
            found, shadows = self._local_method(self.classes[key], name)
            if shadows:
                return found, path
        return [], path

    def _find_method(self, cls: dict, name: str, seen: set[str] | None = None, include_self: bool = True) -> tuple[list[dict], list[str]]:
        order, reason = self._mro(cls)
        if reason != 'complete':
            # A direct ordinary method can still be a grounded may-call on an
            # unknown base, but invalid/customized classes are not assumed valid.
            if reason == 'unknown_base' and include_self:
                local, _ = self._local_method(cls, name)
                return local, [cls['canonical_name'], reason]
            return [], [cls['canonical_name'], reason]
        return self._lookup_mro(order if include_self else order[1:], name)

    def _resolve_super(self, call: dict, owner: dict | None, imports: dict) -> tuple[list[dict], str, list[str]]:
        """Resolve super after its anchor in each source-backed receiver MRO.

        Method bodies are context-insensitive: self may be an instance of a
        known subclass, so cooperative sibling dispatch is a may-call union.
        Read original source because legacy callee_text erases call arguments.
        """
        fn = self.by_id.get(call.get('caller_id'), {})
        if not owner or fn.get('scope') != owner['qualified_name']:
            return [], 'super_context_unknown', []
        if ('super' in imports or 'super' in fn.get('parameters', []) or
                self._lookup(call['module'] + '.super') or self._find_binding(call, 'super')):
            return [], 'super_shadowed', []
        if (self._lookup(call['module'] + '.' + call['scope'] + '.super') or
                any('super' in a.get('targets', [])
                    for a in self.assignments.get((call['file'], call['scope']), []))):
            return [], 'super_shadowed', []
        try:
            expr = ast.parse(call.get('source') or '', mode='eval').body
        except SyntaxError:
            return [], 'super_syntax_unknown', []
        if not (isinstance(expr, ast.Call) and isinstance(expr.func, ast.Attribute)
                and isinstance(expr.func.value, ast.Call)):
            return [], 'super_syntax_unknown', []
        proxy = expr.func.value
        if not isinstance(proxy.func, ast.Name) or proxy.func.id != 'super' or proxy.keywords:
            return [], 'super_syntax_unknown', []
        params = fn.get('parameters', [])
        anchor = owner
        if not params or 'staticmethod' in fn.get('decorators', []):
            return [], 'super_receiver_unknown', []
        receiver = params[0]
        if self._find_binding(call, receiver):
            return [], 'super_receiver_rebound', []
        if proxy.args:
            if len(proxy.args) != 2 or not isinstance(proxy.args[1], ast.Name) or proxy.args[1].id != receiver:
                return [], 'super_receiver_unknown', []
            refs = self._resolve_reference(ast.unparse(proxy.args[0]), call['module'], imports)
            if len(refs) != 1 or refs[0]['kind'] != 'CLASS':
                return [], 'super_anchor_unknown', []
            anchor = refs[0]
        targets, trace = [], []
        for cls in self.classes.values():
            order, status = self._mro(cls)
            if status != 'complete' or owner['canonical_name'] not in order or anchor['canonical_name'] not in order:
                continue
            tail = order[order.index(anchor['canonical_name']) + 1:]
            found, path = self._lookup_mro(tail, expr.func.attr)
            targets.extend(found)
            if found:
                trace.extend(['receiver:' + cls['canonical_name'], 'anchor:' + anchor['canonical_name'], *path])
        return targets, 'super_c3_may_call' if targets else 'super_method_not_found', trace

    def _resolve_reference(self, name: str, module: str, imports: dict[str, str]) -> list[dict]:
        # Imported names must not fall back to accidental same-name definitions.
        parts = name.split('.')
        if parts[0] in imports:
            candidate = '.'.join([imports[parts[0]], *parts[1:]])
            return self.origins.project_symbols(candidate)
        return self._lookup(module + '.' + name)

    def _find_binding(self, call: dict, var: str) -> dict | None:
        # Nearest lexical scope, most recent preceding binding.
        scope = call['scope']
        scopes = []
        parts = scope.split('.')
        while parts and scope != '<module>':
            scopes.append('.'.join(parts))
            parts.pop()
        scopes.append('<module>')
        for scope_candidate in scopes:
            prior = [a for a in self.assignments.get((call['file'], scope_candidate), [])
                     if var in a.get('targets', [])
                     and (a.get('line') or 0, a.get('column') or 0) <
                     (call.get('line') or 0, call.get('column') or 0)]
            if prior:
                return prior[-1]
        return None

    def _resolve_binding(self, call: dict, var: str, imports: dict[str, str], visited: set[str] | None = None) -> tuple[list[dict], str]:
        visited = set() if visited is None else visited
        if var in visited:
            return [], 'circular_alias'
        visited.add(var)
        bind = self._find_binding(call, var)
        if bind is None:
            return [], 'no_local_binding'
        # A binding in a branch is not necessarily the value reaching a call.
        if bind.get('control_depth', 0) and bind.get('type') != 'FOR_TARGET':
            return [], 'branch_assignment_requires_control_flow_analysis'
        b = bind.get('binding') or {}
        if b.get('kind') == 'call':
            refs = self._resolve_reference(b.get('callee') or '', call['module'], imports)
            return [s for s in refs if s['kind'] == 'CLASS'], 'assignment_constructor'
        if b.get('kind') == 'name':
            name = b.get('name')
            refs = self._resolve_reference(name or '', call['module'], imports)
            if refs:
                return refs, 'assignment_alias'
            return self._resolve_binding(call, name or '', imports, visited)
        if b.get('kind') == 'sequence':
            vals = []
            for name in b.get('items', []):
                vals.extend(self._resolve_reference(name, call['module'], imports))
            return vals, 'sequence_elements'
        if b.get('kind') == 'iterate':
            source = b.get('source')
            if source:
                vals, detail = self._resolve_binding(call, source, imports, visited)
                return vals, 'loop_over_' + detail
        return [], 'unknown_binding_kind'

    def _flow_targets(self, call: dict, var: str, imports: dict[str, str],
                      *, method_receiver: bool) -> tuple[list[dict], str, list[str]] | None:
        """Resolve at the call's program point (not last AST assignment).

        None means no structured-flow snapshot was recorded and the legacy
        fallback is needed. An empty target list means a real snapshot was
        recorded but the binding is unresolvable or not defined.
        """
        flow = call.get('flow')
        if flow is None or flow.get('variable') != var:
            return None
        binding = flow.get('binding')
        if binding is None:
            return [], 'flow_variable_unbound', []
        targets, trace, contains_unknown = [], [], False
        annotated = False
        for option in binding.get('options', []):
            kind, name = option.get('kind'), option.get('name') or ''
            if kind == 'unknown':
                contains_unknown = True
                continue
            if kind in ('sequence_reference', 'sequence_constructed', 'sequence_unknown',
                        'dictionary_reference', 'dictionary_unknown'):
                # A list/set is not an instance of an element class.
                contains_unknown = True
                continue
            if kind == 'constructed' and not method_receiver:
                # Calling an object is not equivalent to calling its class.
                contains_unknown = True
                continue
            if kind == 'annotated_parameter':
                # Parameter annotations describe *instances*, not class
                # constructors, and Python does not enforce them at runtime.
                if not method_receiver:
                    contains_unknown = True
                    continue
                annotated = True
            refs = self._resolve_reference(name, call['module'], imports)
            if kind == 'annotated_parameter':
                refs = [r for r in refs if r['kind'] == 'CLASS']
                # Ambiguous or ungrounded hints cannot prove a project class.
                if len(refs) != 1:
                    contains_unknown = True
                    continue
            if not refs and kind == 'reference' and '.' not in name:
                # Nested functions live in lexical scopes, not module scope.
                parts = (call.get('scope') or '').split('.')
                while parts and not refs:
                    refs = self._lookup(call['module'] + '.' + '.'.join(parts) + '.' + name)
                    parts.pop()
            if method_receiver:
                refs = [r for r in refs if r['kind'] == 'CLASS']
            if not refs:
                contains_unknown = True
            targets.extend(refs)
            trace.append(f"{kind}:{name}@{option.get('line')}")
        targets = list({t['id']: t for t in targets}.values())
        if not targets:
            return [], 'flow_unknown_or_external', trace
        if contains_unknown:
            rule = 'flow_partial_type_candidates'
        elif binding.get('may_be_unbound'):
            rule = 'flow_maybe_unbound'
        elif len(targets) > 1:
            rule = ('flow_collection_candidates' if any(o.get('from_collection') for o in binding['options'])
                    else 'flow_merged_candidates')
        elif annotated:
            rule = 'parameter_type_hint'
        else:
            rule = ('flow_collection_single_candidate' if any(o.get('from_collection') for o in binding['options'])
                    else 'flow_reaching_assignment')
        return targets, rule, trace

    def resolve(self, call: dict) -> tuple[list[dict], str, list[str]]:
        callee = call.get('callee_text') or ''
        imports = self.imports_at(call)
        owner = self._owner_class(call)
        if callee.startswith('super().'):
            return self._resolve_super(call, owner, imports)
        if callee.startswith(('self.', 'cls.')):
            if owner and callee.count('.') == 1:
                meth = callee.split('.', 1)[1]
                found, trace = self._find_method(owner, meth)
                return found, ('self_inheritance' if len(trace) > 1 else 'self_method') if found else 'unknown_self_method', trace
            return [], 'chained_attribute_not_supported', []
        if call.get('syntax_type') == 'NAME':
            flow_result = self._flow_targets(call, callee, imports, method_receiver=False)
            if flow_result is not None and (call.get('flow') or {}).get('binding') is not None:
                return flow_result
            if flow_result is not None and self._find_binding(call, callee):
                return flow_result  # no reaching definition; avoid future/branch guesses
            if flow_result is None:
                binding = self._find_binding(call, callee)
                if binding:
                    found, why = self._resolve_binding(call, callee, imports)
                    return found, why if found else 'local_' + why, [f"local:{callee}"]
            # Do not mistake a parameter for a same-named project function.
            current_fn = self.by_id.get(call['caller_id'], {})
            if callee in current_fn.get('parameters', []):
                return [], 'parameter_callable_unknown', []
            # Local definitions and enclosing function definitions.
            scope = call['scope']
            if scope and scope != '<module>':
                parts = scope.split('.')
                while parts:
                    nested = self._lookup(call['module'] + '.' + '.'.join(parts) + '.' + callee)
                    if nested:
                        return nested, 'lexical_nested_function', ['.'.join(parts)]
                    parts.pop()
            refs = self._resolve_reference(callee, call['module'], imports)
            if refs:
                return refs, 'import_or_same_module', [callee]
            return [], 'unresolved_name_builtin_external_or_dynamic', []
        if '.' in callee:
            left, rest = callee.split('.', 1)
            refs = self._resolve_reference(left, call['module'], imports)
            class_refs = [r for r in refs if r['kind'] == 'CLASS']
            if class_refs:
                matches = []
                traces = []
                for cls in class_refs:
                    ms, trace = self._find_method(cls, rest)
                    matches.extend(ms)
                    traces += trace
                return matches, 'explicit_class_method' if matches else 'class_method_missing', traces
            flow_result = self._flow_targets(call, left, imports, method_receiver=True)
            if flow_result is not None:
                instances, why, origins = flow_result
                matches, traces = [], []
                for cls in instances:
                    ms, path = self._find_method(cls, rest)
                    matches.extend(ms)
                    traces.extend(path)
                if matches:
                    return matches, 'instance_' + why, origins + traces
                # A real assignment exists, but points only to unknown or
                # external candidates. Do not guess based on variable name.
                if (call.get('flow') or {}).get('binding') is not None:
                    return [], 'instance_method_unresolved', origins
            else:
                bind = self._find_binding(call, left)
                if bind:
                    instances, why = self._resolve_binding(call, left, imports)
                    matches, traces = [], []
                    for cls in instances:
                        if cls['kind'] == 'CLASS':
                            ms, trace = self._find_method(cls, rest)
                            matches.extend(ms)
                            traces += trace
                    return matches, 'instance_' + why if matches else 'instance_method_unresolved', traces
            # Imported module aliases: module.function / module.Class.method.
            if left in imports:
                full_name = imports[left] + '.' + rest
                found = self.origins.project_symbols(full_name)
                if found:
                    return found, 'imported_module', [imports[left]]
                # Imported class? attempted above; imported module path with a class method.
                if '.' in rest:
                    cls_part, meth = rest.rsplit('.', 1)
                    cls = self.classes.get(imports[left] + '.' + cls_part)
                    if cls:
                        found, trace = self._find_method(cls, meth)
                        return found, 'imported_class_method', trace
            # locally import without explicit alias, e.g. package.module.func
            local = self._lookup(call['module'] + '.' + callee)
            if local:
                return local, 'same_module_qualified', [callee]
            return [], 'external_or_dynamic_attribute', []
        return [], 'unsupported_dynamic_syntax', []

    def _module_binding_at_call(self, call: dict, targets: list[dict]) -> list[dict]:
        """Select the live, straight-line local definition at module evaluation.

        Only same-file, unconditional duplicate functions are disambiguated.
        Deferred function bodies cannot be resolved by comparing source lines.
        """
        result = []
        for target in targets:
            group = self._lookup(target['canonical_name'])
            versions = [s for s in group if s['kind'] == target['kind']
                        and s['file'] == call['file'] and s['module'] == call['module']]
            if len(versions) <= 1 or not all(s.get('control_depth', 0) == 0 for s in versions):
                result.append(target)
                continue
            # A function is bound *after* its defaults/decorators run. Calls
            # on its own declaration line must see the previous definition.
            prior = [s for s in versions if (s.get('line') or 0) < (call.get('line') or 0)]
            if call.get('execution_phase') not in ('definition_default', 'definition_decorator',
                                                     'definition_annotation'):
                prior.extend(s for s in versions
                             if (s.get('line') or 0) == (call.get('line') or 0)
                             and (s.get('column') or 0) < (call.get('column') or 0))
            if prior:
                result.append(max(prior, key=lambda s: (s.get('line') or 0, s.get('column') or 0)))
            # Otherwise the call predates all definitions and is unresolved.
        return list({s['id']: s for s in result}.values())

    def build(self) -> dict:
        from object_flow import ObjectFlow
        object_flow = ObjectFlow(self, fields=True, dispatch=True)
        object_results = object_flow.run()
        self.return_summaries = object_flow.summaries
        self.object_flow_stats = {'expression_steps': object_flow.steps,
                                  'root_step_limit': object_flow.root_step_limit,
                                  'recursion_or_budget_cutoffs': object_flow.cutoffs,
                                  'generic_context_fallbacks': object_flow.generic_context_fallbacks,
                                  'summarized_functions': len(object_flow.summaries),
                                  'managed_calls': len(object_results)}
        nodes = [{"id": s['id'], "kind": s['kind'], "name": s['name'],
                  "canonical_name": s['canonical_name'], "file": s['file'],
                  "line": s.get('line')} for s in self.symbols]
        # A module-level call has a real caller: module initialization. Keep
        # module nodes separate from function nodes, without guessing external
        # package implementations or fabricating a user function caller.
        module_files = {s['module']: s['file'] for s in self.symbols}
        module_files.update({c['module']: c['file'] for c in self.calls})
        module_nodes = {m: 'MODULE:' + m for m in module_files}
        for module in sorted(module_files):
            nodes.append({'id': module_nodes[module], 'kind': 'MODULE',
                          'name': module.split('.')[-1], 'canonical_name': module,
                          'file': module_files[module], 'line': 1})
        # Prevent dangling/ambiguous graph identities even if input facts
        # contain repeated definitions from conditional declarations.
        nodes = list({node['id']: node for node in nodes}.values())
        edges, unresolved, ambiguous, conditional = [], [], [], []
        for s in self.symbols:
            if s['kind'] != 'METHOD':
                continue
            cls = self.classes.get(s['module'] + '.' + s['scope'])
            if cls:
                edges.append({"source": cls['id'], "target": s['id'], "type": "CONTAINS",
                              "evidence": {"file": s['file'], "line": s['line'], "rule": "method_scope"}})
        for cls in self.classes.values():
            for base in self._get_bases(cls):
                edges.append({"source": cls['id'], "target": base['id'], "type": "INHERITS",
                              "evidence": {"file": cls['file'], "line": cls['line'], "rule": "declared_base"}})
        resolved_sites = set()
        for call in self.calls:
            top_level = not call.get('caller_id')
            caller_id = call.get('caller_id') or module_nodes[call['module']]
            targets, reason, trace = object_results.get(call['id'], (None, None, None))
            if targets is None:
                targets, reason, trace = self.resolve(call)
            elif targets:
                legacy_targets, legacy_reason, legacy_trace = self.resolve(call)
                if {t['id'] for t in targets} == {t['id'] for t in legacy_targets}:
                    # Preserve established flow provenance for unchanged edges.
                    targets, reason, trace = legacy_targets, legacy_reason, legacy_trace
            # A proven-invalid C3 hierarchy cannot be instantiated. Unknown
            # external bases still permit a source-backed class candidate.
            targets = [t for t in targets if not (
                t['kind'] == 'CLASS' and self._mro(t)[1].startswith('invalid'))]
            if top_level:
                # Module initialization and definition-time expressions execute
                # before later definitions are bound. Preserve an earlier
                # definition even when the canonical name is later redefined.
                targets = self._module_binding_at_call(call, targets)
                targets = [t for t in targets if not (
                    t['module'] == call['module'] and (t.get('line') or 0) > (call.get('line') or 0))]
            # Repeated targets from two aliases don't create duplicate edges.
            targets = list({s['id']: s for s in targets}.values())
            if not targets:
                item = {"call_id": call['id'], "callee_text": call['callee_text'],
                        "file": call['file'], "line": call['line'],
                        "reason": 'no_enclosing_function' if top_level else reason,
                        "execution_scope": call.get('execution_scope', call.get('scope')),
                        "execution_phase": call.get('execution_phase', 'unspecified')}
                if not top_level:
                    item['caller_id'] = caller_id
                if top_level:
                    item['resolution_reason'] = reason
                unresolved.append(item)
                continue
            resolved_sites.add(call['id'])
            if len(targets) > 1:
                ambiguous.append({"call_id": call['id'], "callee_text": call['callee_text'],
                                  "targets": [s['id'] for s in targets], "type": "may_call_candidates"})
            flow = call.get('flow') or {}
            bound = flow.get('binding') or {}
            may_unbound = bool(bound.get('may_be_unbound'))
            may_unknown = ('partial_type_candidates' in reason or any(o.get('kind') == 'unknown' for o in bound.get('options', [])))
            evidence = {"call_id": call['id'], "file": call['file'], "line": call['line'],
                        "column": call.get('column'), "source": call.get('source'),
                        "rule": reason, "trace": trace, "module_level": top_level,
                        "execution_scope": call.get('execution_scope', call.get('scope')),
                        "execution_phase": call.get('execution_phase', 'unspecified')}
            if reason == 'super_c3_may_call':
                evidence['certainty'] = 'receiver_hierarchy_may_call'
            if reason == 'object_flow_may_call':
                evidence['certainty'] = 'interprocedural_may_call'
            if top_level:
                evidence['certainty'] = 'module_initialization_may_execute'
            if call.get('execution_phase') == 'definition_annotation':
                evidence['certainty'] = 'annotation_evaluation_version_dependent'
            if len(targets) > 1 and 'certainty' not in evidence:
                evidence['certainty'] = 'multiple_possible_targets'
            if reason.startswith(('instance_flow_', 'flow_')):
                evidence['certainty'] = ('conditional_may_call' if may_unbound or may_unknown
                                         else 'multiple_possible_targets' if len(targets) > 1 else 'definite_within_supported_flow')
                evidence['reaching_definitions'] = bound.get('options', [])
                evidence['may_be_unbound'] = may_unbound
            if reason.startswith('instance_') and any(
                    opt.get('kind') == 'annotated_parameter'
                    for opt in bound.get('options', [])):
                # An annotation is advisory and subclasses may override the
                # method. This is a source-backed candidate, not definite
                # runtime dispatch or complete polymorphic target coverage.
                evidence['certainty'] = 'declared_parameter_type_may_call'
                evidence['reaching_definitions'] = bound.get('options', [])
                evidence['annotation_note'] = ('Parameter hints are not enforced at runtime; '
                                               'subclass dispatch and reassignment may change targets.')
            if may_unbound and reason.startswith(('instance_flow_', 'flow_')):
                conditional.append({"call_id": call['id'], "caller_id": caller_id,
                                    "callee_text": call['callee_text'], "file": call['file'],
                                    "line": call['line'], "target_ids": [t['id'] for t in targets],
                                    "reason": "may_be_unbound_on_some_paths"})
            for target in targets:
                t = 'INSTANTIATES' if target['kind'] == 'CLASS' else 'CALLS'
                edges.append({"source": caller_id, "target": target['id'],
                              "type": t, "evidence": evidence})
                if target['kind'] == 'CLASS':
                    init, path = self._find_method(target, '__init__')
                    for ctor in init:
                        edges.append({"source": caller_id, "target": ctor['id'],
                                      "type": "CALLS", "evidence": {**evidence, "rule": "constructor_init", "trace": path}})
        # Each call relation is a unique (caller, callee, type, call-site) fact.
        edge_map = {}
        for edge in edges:
            ev = edge.get('evidence') or {}
            key = (edge['source'], edge['target'], edge['type'], ev.get('call_id'))
            edge_map.setdefault(key, edge)
        edges = list(edge_map.values())
        module_sites = {edge['evidence']['call_id'] for edge in edges
                        if edge.get('evidence', {}).get('call_id') and edge['evidence'].get('module_level')}
        collection_sites = {edge['evidence']['call_id'] for edge in edges
                            if edge.get('evidence', {}).get('call_id') and
                            'flow_collection_' in edge['evidence'].get('rule', '')}
        return {"repository": self.data.get('repository'),
                "node_count": len(nodes), "edge_count": len(edges),
                "total_call_sites": len(self.calls), "resolved_call_sites": len(resolved_sites),
                "unresolved_call_sites": len(unresolved),
                "module_level_resolved_sites": len(module_sites),
                "collection_indirect_resolved_sites": len(collection_sites),
                "nodes": nodes, "edges": edges,
                "object_flow_stats": self.object_flow_stats,
                "unresolved_calls": unresolved, "ambiguous_calls": ambiguous,
                "conditional_calls": conditional}
