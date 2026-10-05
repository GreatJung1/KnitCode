"""v2.3.1: Conservative call-site boundary reporting (not a new resolver).

Every AST call site gets exactly one status.  Only unshadowed, unconditional
imports with a single non-project provenance and unshadowed builtins are marked
KNOWN_NON_INTERNAL. Legacy unresolved categories remain *heuristic* and intact.
Target Python modules are parsed, never imported or executed.
"""
from __future__ import annotations

from collections import Counter, defaultdict
import builtins
import csv
from pathlib import Path

from import_resolution import ImportOrigins, canonical_import
from join_origin import literal_join_receiver
from builtin_receivers import builtin_method_origin
from symbol_builder import save_json

STATES = ('RESOLVED_INTERNAL', 'KNOWN_NON_INTERNAL', 'UNKNOWN')
NON_INTERNAL = {'builtin': 'BUILTIN', 'stdlib': 'STDLIB', 'external': 'EXTERNAL_IMPORT'}


def visible_scopes(scope):
    scope = scope or '<module>'
    if scope == '<module>':
        return ['<module>']
    segments = scope.split('.')
    return ['<module>'] + ['.'.join(segments[:n]) for n in range(1, len(segments) + 1)]


class StatusAnnotator:
    def __init__(self, symbols):
        self.symbols = symbols
        self.origins = ImportOrigins(symbols)
        self.has_binding_hazards = 'binding_hazards' in symbols
        self.defs = symbols.get('symbols', [])
        self.imports = defaultdict(list)
        self.star_imports = defaultdict(list)
        self.assignments = defaultdict(list)
        self.declarations = defaultdict(list)
        self.params = defaultdict(set)
        self.hazards = defaultdict(list)
        for h in symbols.get('binding_hazards', []):
            self.hazards[(h['file'], h['name'])].append(h)
        for item in symbols.get('imports', []):
            self.imports[(item['file'], item.get('scope') or '<module>', item.get('local_name'))].append(item)
            if item.get('local_name') == '*':
                self.star_imports[item['file']].append(item)
        for item in symbols.get('assignments', []):
            for name in item.get('targets', []):
                self.assignments[(item['file'], item.get('scope') or '<module>', name)].append(item)
        # Only project-defined classes; no runtime imports or attribute access.
        self.local_classes = {(d.get('module'), d.get('qualified_name')): d
                              for d in self.defs if d.get('kind') == 'CLASS'}
        self.local_methods = {(d.get('module'), d.get('qualified_name'))
                              for d in self.defs if d.get('kind') == 'METHOD'}
        self.class_assignments = {(a['file'], a.get('scope'), t)
                                  for a in symbols.get('assignments', [])
                                  for t in a.get('targets', [])}
        for item in self.defs:
            scope = item.get('scope') or '<module>'
            self.declarations[(item['file'], scope, item['name'])].append(item)
            if item.get('kind') in ('FUNCTION', 'METHOD'):
                self.params[(item['file'], item.get('qualified_name'))].update(item.get('parameters', []))

    def _bound_at(self, call, name):
        """Return an eligible import and ALL potentially competing bindings.

        Conservatively reject names with reassignment, nested shadowing, or
        conditional imports. Without CFG, 'no reported conflicts' != proof that
        Python runtime namespaces cannot mutate.
        """
        file, line = call['file'], call.get('line') or 0
        scopes = visible_scopes(call.get('scope'))
        imports, conflicts = [], []
        for star in self.star_imports.get(file, []):
            if (star.get('scope') or '<module>') in scopes:
                conflicts.append({'type': 'wildcard_import_may_shadow', 'line': star.get('line')})
        for hazard in self.hazards.get((file, name), []):
            # A 'global' in any other function can mutate a module-level
            # import between calls. Other rebinding constructs matter only
            # inside a visible lexical scope.
            if hazard['type'] in ('GLOBAL', 'NONLOCAL') or hazard.get('scope') in scopes:
                conflicts.append({'type': hazard['type'], 'line': hazard.get('line'),
                                  'scope': hazard.get('scope')})
        for scope in scopes:
            key = (file, scope, name)
            for imp in self.imports.get(key, []):
                # Function-local imports below the call are not yet bound. A
                # module-level import below a function body could run earlier
                # than invocation; treat it as uncertain, never assume safety.
                if scope == '<module>' and call.get('scope') != '<module>':
                    if (imp.get('line') or 0) > line:
                        conflicts.append({'type': 'module_import_order_unknown', 'line': imp.get('line')})
                        continue
                elif (imp.get('line') or 0) > line:
                    continue
                imports.append(imp)
                if imp.get('control_depth', 0):
                    conflicts.append({'type': 'conditional_import', 'line': imp.get('line')})
            for ass in self.assignments.get(key, []):
                # All assignments in a function's scope may shadow an import
                # at runtime, including control-flow alternatives.
                conflicts.append({'type': 'assignment', 'line': ass.get('line'),
                                  'control_depth': ass.get('control_depth', 0)})
            for dec in self.declarations.get(key, []):
                conflicts.append({'type': 'definition', 'line': dec.get('line')})
            if name in self.params.get((file, scope), set()):
                conflicts.append({'type': 'parameter', 'scope': scope})
        if len(imports) > 1:
            conflicts.append({'type': 'competing_imports', 'count': len(imports)})
        return imports[0] if len(imports) == 1 else None, conflicts, bool(imports)

    def non_internal_origin(self, call):
        # Legacy v2.3 symbol_result.json lacks rebinding-hazard facts.
        # A standalone refresh must never label origins as verified on
        # structurally incomplete provenance input.
        if not self.has_binding_hazards:
            return None, 'legacy_symbols_missing_rebinding_hazards', {}
        callee = call.get('callee_text') or ''
        head, sep, tail = callee.partition('.')
        if not head.isidentifier():
            return None, 'computed_or_dynamic_receiver', {}
        imp, conflicts, saw_import = self._bound_at(call, head)
        if conflicts:
            return None, 'shadowing_or_conditional_binding', {'conflicts': conflicts[:12]}
        if imp:
            name = canonical_import(imp)
            origins = self.origins.trace(name)
            kinds = {o.get('kind') for o in origins}
            evidence = {'kind': 'import_provenance', 'binding': head,
                        'import_line': imp.get('line'), 'import_target': name,
                        'origin_candidates': [{'kind': o['kind'], 'name': o['name']}
                                              for o in origins]}
            if len(kinds) == 1 and next(iter(kinds), None) in NON_INTERNAL:
                kind = next(iter(kinds))
                # Plain import aliases may be modules. The exact attribute is
                # NOT verified; provenance is to a non-project root, only.
                evidence['note'] = ('External provenance of receiver/name only; '
                                    'attribute existence and dynamic rebinding are unverified.')
                evidence['origin_name'] = ', '.join(sorted({o['name'] + ('.' + tail if tail else '') for o in origins}))
                return NON_INTERNAL[kind], 'UNSHADOWED_IMPORT_ORIGIN', evidence
            return None, 'mixed_or_unproven_import_origin', evidence
        if saw_import:
            return None, 'competing_imports', {}
        # Even a builtin can be rebound by user code outside this module at
        # runtime, but this is a safe static *lexical* builtin-origin claim.
        if not sep and head in vars(builtins):
            return 'BUILTIN', 'UNSHADOWED_BUILTIN_NAME', {
                'kind': 'builtin_namespace', 'origin_name': 'builtins.' + head,
                'note': 'Lexical builtin binding only; runtime namespace mutation is out of scope.'}
        return None, 'no_supported_provenance', {}

    def _stdlib_join_constructor(self, call, typename):
        """Recognize only known external types after safe import provenance."""
        if not typename or '.' not in typename:
            return None
        head, tail = typename.split('.', 1)
        imp, conflicts, _ = self._bound_at(call, head)
        if conflicts or not imp:
            return None
        canonical = canonical_import(imp)
        kinds = {o.get('kind') for o in self.origins.trace(canonical)}
        if kinds != {'stdlib'}:
            return None
        # Avoid mistaking other (possibly dynamic) classes for a stdlib join.
        fullname = canonical + '.' + tail
        if fullname not in ('threading.Thread', 'multiprocessing.Process'):
            return None
        # Source-level module attribute rebinding invalidates a confident
        # claim about which constructor or join method is invoked.
        if any(file == call['file'] and (
               target == typename or target == head + '.' + tail + '.join')
               for file, _scope, target in self.class_assignments):
            return None
        return fullname

    def _inherits_stdlib_join(self, call, name, visited=None):
        """Check all earlier bases to avoid relying on an unknown MRO entry."""
        visited = visited or set()
        if self._stdlib_join_constructor(call, name):
            return True
        key = (call.get('module'), name)
        definition = self.local_classes.get(key)
        if not definition or key in visited:
            return False
        visited = visited | {key}
        # A local method or class-attribute assignment can override .join().
        if (key[0], name + '.join') in self.local_methods or (definition['file'], name, 'join') in self.class_assignments or (definition['file'], name, name + '.join') in self.class_assignments:
            return False
        bases = definition.get('bases') or []
        # Python MRO searches bases left to right. If the first base is not
        # proven, a later stdlib base cannot be certified as selected.
        return bool(bases and self._inherits_stdlib_join(call, bases[0], visited))

    def join_origin(self, call):
        """Built-in literal or all flow candidates sourced from known stdlib join."""
        literal = literal_join_receiver(call)
        if literal:
            return 'BUILTIN', 'LITERAL_BUILTIN_JOIN', {
                'kind': 'literal_receiver', 'origin_name': f'{literal}.join',
                'note': 'Literal str/bytes receiver, verified directly from call AST.'}

        if not self.has_binding_hazards:
            # Legacy symbol snapshots lack the facts needed to vet shadowing.
            return None
        callee = call.get('callee_text') or ''
        parts = callee.split('.')
        if len(parts) != 2 or parts[1] != 'join' or not parts[0].isidentifier():
            return None
        flow = call.get('flow') or {}
        if flow.get('variable') != parts[0]:
            return None
        # Assigning an instance-specific join method (possibly conditionally)
        # overrides the inherited method even if its class is known.
        if any(file == call['file'] and target == parts[0] + '.join'
               for file, _scope, target in self.class_assignments):
            return None
        binding = flow.get('binding') or {}
        options = binding.get('options') or []
        if binding.get('may_be_unbound') or not options:
            return None
        for option in options:
            if option.get('kind') != 'constructed':
                return None
            name = option.get('name')
            if not (self._stdlib_join_constructor(call, name) or
                    self._inherits_stdlib_join(call, name)):
                return None
        return 'STDLIB', 'FLOW_STDLIB_JOIN', {
            'kind': 'flow_constructor_and_inheritance',
            'origin_name': 'threading.Thread.join or multiprocessing.Process.join',
            'receiver_candidates': sorted({x.get('name') for x in options}),
            'note': ('All tracked constructor candidates inherit a stdlib join method; '
                     'dynamic reassignment and monkeypatching are outside the model.')}

    def builtin_receiver_origin(self, call):
        """Prove supported built-in method receivers, never guessing by name."""
        if not self.has_binding_hazards:
            return None
        # Built-in constructors may be shadowed by project declarations,
        # imports or parameters. A constructor-looking name is not enough.
        def safe_constructor(name):
            imp, conflicts, saw_import = self._bound_at(call, name)
            return (not imp and not saw_import and not conflicts
                    and name in ('str', 'bytes', 'list', 'dict', 'set', 'tuple'))
        def override(receiver, method):
            return any(file == call['file'] and target in (receiver + '.' + method,)
                       for file, _scope, target in self.class_assignments)
        inferred = builtin_method_origin(call, is_unshadowed_builtin=safe_constructor,
                                         has_receiver_override=override)
        if not inferred:
            return None
        kind, rule = inferred
        return 'BUILTIN', rule, {
            'kind': 'builtin_receiver_type', 'origin_name': kind + '.' +
            str((call.get('callee_text') or '').split('.')[-1]),
            'note': ('Source-backed receiver type in supported flow; dynamic mutation '
                     'and monkeypatching outside the model.')}

    def annotate(self, graph):
        edge_ids = defaultdict(list)
        for edge in graph.get('edges', []):
            call_id = (edge.get('evidence') or {}).get('call_id')
            if call_id:
                edge_ids[call_id].append(edge)
        unresolved = {x['call_id']: x for x in graph.get('unresolved_calls', [])}
        conditional = {x['call_id'] for x in graph.get('conditional_calls', [])}
        ambiguous = {x['call_id'] for x in graph.get('ambiguous_calls', [])}
        rows = []
        for call in self.symbols.get('calls', []):
            cid = call['id']
            edges = edge_ids.get(cid, [])
            legacy = unresolved.get(cid)
            targets = sorted({e['target'] for e in edges if e['type'] == 'CALLS' or e['type'] == 'INSTANTIATES'})
            row = {'call_id': cid, 'file': call['file'], 'line': call.get('line'),
                   'callee_text': call.get('callee_text'), 'caller_id': call.get('caller_id'),
                   'scope_context': 'IN_FUNCTION' if call.get('caller_id') else 'MODULE_LEVEL',
                   'target_ids': targets, 'legacy_category': legacy.get('category') if legacy else None}
            if edges:
                # This means the analyzer found a source-backed may-call edge,
                # NOT that all runtime implementations are represented.
                partial = (cid in conditional or cid in ambiguous or
                           any('partial_type_candidates' in (e.get('evidence', {}).get('rule') or '') or
                               e.get('evidence', {}).get('may_be_unbound') or
                               e.get('evidence', {}).get('certainty') == 'declared_parameter_type_may_call'
                               for e in edges))
                row.update(status='RESOLVED_INTERNAL', origin_kind='INTERNAL',
                           certainty='MAY_CALL' if partial else 'SOURCE_BACKED',
                           evidence={'kind': 'graph_edges', 'rules': sorted({
                               (e.get('evidence') or {}).get('rule') for e in edges}),
                               'coverage_caveat': ('Partial/multiple/conditional candidates.' if partial else
                                                   'May-call targets; completeness and runtime dispatch not proven.')})
            else:
                category, rule, evidence = self.non_internal_origin(call)
                certainty = 'PROVEN_STATIC_ORIGIN'
                if category is None:
                    inferred = self.join_origin(call)
                    if inferred:
                        category, rule, evidence = inferred
                        if rule == 'FLOW_STDLIB_JOIN':
                            certainty = 'INFERRED_STATIC_ORIGIN'
                if category is None:
                    inferred = self.builtin_receiver_origin(call)
                    if inferred:
                        category, rule, evidence = inferred
                if category:
                    row.update(status='KNOWN_NON_INTERNAL', origin_kind=category,
                               certainty=certainty, evidence={'rule': rule, **evidence})
                else:
                    row.update(status='UNKNOWN', origin_kind='UNKNOWN', certainty='UNVERIFIED',
                               evidence={'rule': rule, **evidence,
                                         'note': 'Legacy category is heuristic, not a verified origin.'})
            rows.append(row)
        counts = Counter(x['status'] for x in rows)
        origins = Counter(x['origin_kind'] for x in rows if x['status'] == 'KNOWN_NON_INTERNAL')
        graph['call_statuses'] = rows
        graph['status_summary'] = {
            'total': len(rows),
            'statuses': {name: counts.get(name, 0) for name in STATES},
            'known_non_internal_origins': {name: origins.get(name, 0) for name in ('BUILTIN', 'STDLIB', 'EXTERNAL_IMPORT')},
            'warning': ('Source-backed means evidence for static provenance or may-call edges, '
                        'not proof of executable target, soundness, completeness, or accuracy. '
                        'UNKNOWN is not equivalent to a missed internal call.'),
        }
        assert len(rows) == graph['total_call_sites'] == len({x['call_id'] for x in rows})
        assert counts['RESOLVED_INTERNAL'] == graph['resolved_call_sites']
        assert counts['KNOWN_NON_INTERNAL'] + counts['UNKNOWN'] == graph['unresolved_call_sites']
        return graph


def annotate_call_statuses(graph, symbols):
    return StatusAnnotator(symbols).annotate(graph)


def write_call_statuses(graph, path):
    path = Path(path)
    path.mkdir(parents=True, exist_ok=True)
    save_json({'repository': graph.get('repository'), 'commit': graph.get('commit'),
               'summary': graph['status_summary'], 'calls': graph['call_statuses']},
              path / 'call_statuses.json')
    columns = ['call_id', 'file', 'line', 'caller_id', 'callee_text', 'scope_context',
               'status', 'origin_kind', 'certainty', 'legacy_category', 'target_ids', 'evidence']
    with (path / 'call_statuses.csv').open('w', encoding='utf-8-sig', newline='') as out:
        writer = csv.DictWriter(out, fieldnames=columns)
        writer.writeheader()
        for item in graph['call_statuses']:
            writer.writerow({column: (', '.join(item.get(column, [])) if column == 'target_ids'
                                      else str(item.get(column, {})) if column == 'evidence'
                                      else item.get(column, '')) for column in columns})
