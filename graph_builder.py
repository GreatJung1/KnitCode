"""Conservative scope-aware, evidence-rich Python call graph resolver.

May-call edges are generated for finite class candidates from a for-loop.
No cross-file name-only guessing: external/unknown calls remain unresolved.
"""
from __future__ import annotations
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

    def _get_bases(self, cls: dict, seen: set[str] | None = None) -> list[dict]:
        imports = self.imports_at({"file": cls['file'], "module": cls['module'], "scope": cls['scope'] or '<module>', "line": cls.get('line') or 0})
        found = []
        for base in cls.get('bases', []):
            if not base:
                continue
            base_candidates = self._resolve_reference(str(base), cls['module'], imports)
            found.extend(s for s in base_candidates if s['kind'] == 'CLASS')
        return found

    def _find_method(self, cls: dict, name: str, seen: set[str] | None = None, include_self: bool = True) -> tuple[list[dict], list[str]]:
        seen = set() if seen is None else seen
        if cls['id'] in seen:
            return [], []
        seen.add(cls['id'])
        path = [cls['canonical_name']]
        if include_self:
            local = [s for s in self._lookup(cls['canonical_name'] + '.' + name) if s['kind'] == 'METHOD']
            if local:
                return local, path
        for base in self._get_bases(cls):
            found, tail = self._find_method(base, name, seen, include_self=True)
            if found:
                return found, path + tail
        return [], path

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
            if owner:
                meth = callee.split('super().', 1)[1]
                for base in self._get_bases(owner):
                    found, path = self._find_method(base, meth)
                    if found:
                        return found, 'super_parent_method', [owner['canonical_name'], *path]
            return [], 'super_method_not_found', []
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

    def build(self) -> dict:
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
            targets, reason, trace = self.resolve(call)
            if top_level:
                # A module initializer runs in source order: definitions below
                # the call are not yet bound in that module. A full execution
                # simulator is out of scope, but avoid an obvious false edge.
                targets = [t for t in targets if not (
                    t['module'] == call['module'] and (t.get('line') or 0) > (call.get('line') or 0))]
            # Repeated targets from two aliases don't create duplicate edges.
            targets = list({s['id']: s for s in targets}.values())
            if not targets:
                item = {"call_id": call['id'], "callee_text": call['callee_text'],
                        "file": call['file'], "line": call['line'],
                        "reason": 'no_enclosing_function' if top_level else reason}
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
                        "rule": reason, "trace": trace, "module_level": top_level}
            if top_level:
                evidence['certainty'] = 'module_initialization_may_execute'
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
                "unresolved_calls": unresolved, "ambiguous_calls": ambiguous,
                "conditional_calls": conditional}
