"""Evidence-based *triage*, not call resolution or accuracy measurement.

All analyses use already collected AST/symbol facts; this module never imports or
executes analyzed code. Categories are heuristic and do not add graph edges.
"""
from __future__ import annotations

import ast
import builtins
from collections import Counter, defaultdict
import csv
from pathlib import Path
import sys
from import_resolution import canonical_import, ImportOrigins

CATEGORIES = {
    'BUILTIN': 'Python 내장 함수',
    'BUILTIN_METHOD': 'Python 기본 자료형 메서드 추정',
    'STDLIB': '표준 라이브러리',
    'EXTERNAL_IMPORT': '외부 의존성(import 근거)',
    'INTERNAL_CANDIDATE': '내부 호출 후보(미검증)',
    'INDIRECT_CALL': '변수에 담긴 함수/클래스 간접 호출',
    'FIELD_CHAIN': '객체 필드의 연속 접근',
    'RECEIVER_UNKNOWN': '객체 타입 또는 메서드 불명',
    'MODULE_LEVEL': '최상위 호출의 출발점 미지원',
    'DYNAMIC_UNKNOWN': '동적 호출/근거 부족',
}

BASIC_METHODS = {
    'list': {'append', 'extend', 'insert', 'pop', 'remove', 'clear', 'sort', 'reverse', 'copy', 'count', 'index'},
    'dict': {'get', 'setdefault', 'update', 'items', 'keys', 'values', 'pop', 'popitem', 'clear', 'copy'},
    'set': {'add', 'update', 'discard', 'remove', 'union', 'intersection', 'clear', 'copy', 'pop'},
    'str': {'split', 'strip', 'replace', 'lower', 'upper', 'startswith', 'endswith', 'join', 'format', 'find'},
}


def _literal_type(value):
    if not value:
        return None
    try:
        node = ast.parse(value, mode='eval').body
    except (SyntaxError, ValueError, TypeError):
        return None
    if isinstance(node, (ast.List, ast.ListComp)):
        return 'list'
    if isinstance(node, (ast.Dict, ast.DictComp)):
        return 'dict'
    if isinstance(node, (ast.Set, ast.SetComp)):
        return 'set'
    if isinstance(node, (ast.Constant, ast.JoinedStr)) and isinstance(getattr(node, 'value', ''), str):
        return 'str'
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in BASIC_METHODS:
        return node.func.id
    return None


def _scope_parents(scope):
    scope = scope or '<module>'
    if scope == '<module>':
        return ['<module>']
    pieces = scope.split('.')
    return ['<module>'] + ['.'.join(pieces[:i]) for i in range(1, len(pieces) + 1)]


class Classifier:
    def __init__(self, symbols: dict):
        self.calls = {c['id']: c for c in symbols.get('calls', [])}
        self.origins = ImportOrigins(symbols)
        self.definitions = symbols.get('symbols', [])
        self.imports = defaultdict(list)
        self.assignments = defaultdict(list)
        self.functions = {s['id']: s for s in self.definitions}
        self.modules = {s.get('module', '') for s in self.definitions}
        self.modules |= {c.get('module', '') for c in symbols.get('calls', [])}
        self.project_packages = {m.split('.')[0] for m in self.modules if m}
        for imp in symbols.get('imports', []):
            self.imports[imp['file']].append(imp)
        for a in symbols.get('assignments', []):
            for t in a.get('targets', []):
                self.assignments[(a['file'], a.get('scope'), t)].append(a)

    def _imports_at(self, call):
        # Lexical availability of imports; no dynamic import execution.
        parents = set(_scope_parents(call.get('scope')))
        eligible = [i for i in self.imports[call['file']]
                    if i.get('scope') in parents and (i.get('line') or 0) <= (call.get('line') or 0)]
        eligible.sort(key=lambda x: (x.get('line') or 0))
        return {i.get('local_name'): i for i in eligible if i.get('local_name')}

    def _shadowed(self, call, name):
        # A local assignment anywhere in the function shadows a builtin in Python.
        fn = self.functions.get(call.get('caller_id'), {})
        if name in fn.get('parameters', []):
            return True
        file, scope = call['file'], call.get('scope')
        for current in _scope_parents(scope):
            if current != '<module>' or scope == '<module>':
                if self.assignments.get((file, current, name)):
                    return True
        return False

    def _nearest_assignment(self, call, name):
        # For triage only. Even when inferred, branch/reassignment risks remain.
        for scope in reversed(_scope_parents(call.get('scope'))):
            matches = [a for a in self.assignments.get((call['file'], scope, name), [])
                       if (a.get('line') or 0) < (call.get('line') or 0)]
            if matches:
                return max(matches, key=lambda a: (a.get('line') or 0, a.get('column') or 0))
        return None

    def _same_module_def(self, call, name):
        return sorted({d['id'] for d in self.definitions
                       if d.get('module') == call.get('module') and
                       d.get('qualified_name') == name})

    def _target_import(self, imp, head):
        return canonical_import(imp) or imp.get('imported_module') or head

    def _import_kind(self, canonical):
        alternatives = self.origins.trace(canonical)
        classes = {x['kind'] for x in alternatives}
        if classes == {'internal'} or classes == {'internal_module'}:
            return 'INTERNAL_CANDIDATE', 'source_backed_project_import'
        if classes == {'builtin'}:
            return 'BUILTIN', 'traced_builtin_alias'
        if classes <= {'stdlib', 'builtin'}:
            return 'STDLIB', 'traced_stdlib_reexport'
        if classes == {'external'}:
            return 'EXTERNAL_IMPORT', 'traced_external_reexport'
        if classes == {'unknown'}:
            return 'DYNAMIC_UNKNOWN', 'unproven_project_export'
        return 'DYNAMIC_UNKNOWN', 'multiple_or_mixed_import_origins'

    def classify(self, item):
        call = self.calls.get(item.get('call_id')) or {
            'file': item.get('file', ''), 'line': item.get('line'),
            'scope': '<module>' if not item.get('caller_id') else None,
            'caller_id': item.get('caller_id'), 'module': ''
        }
        callee = str(item.get('callee_text') or '')
        head = callee.split('.')[0]
        parts = callee.split('.')
        in_module = not item.get('caller_id')
        imports = self._imports_at(call) if call else {}
        reason = item.get('reason', '')
        evid = {'original_reason': reason}
        scope_context = 'MODULE_LEVEL' if in_module else 'IN_FUNCTION'
        category, rule, certainty = 'DYNAMIC_UNKNOWN', 'insufficient_evidence', 'low'
        candidates = []

        # Import evidence takes priority over names that happen to match builtins.
        if head in imports:
            imp = imports[head]
            module = self._target_import(imp, head)
            category, rule = self._import_kind(module)
            origins = self.origins.trace(module)
            certainty = 'high' if category == 'STDLIB' else 'medium'
            evid.update({'imported_module': (origins[0]['name'].rsplit('.', 1)[0] if origins and category in ('STDLIB', 'EXTERNAL_IMPORT') else module), 'import_line': imp.get('line'),
                         'import_chain': [x.get('via', []) for x in origins],
                         'origin_candidates': [{'kind': x['kind'], 'name': x['name']} for x in origins]})
            if category == 'INTERNAL_CANDIDATE':
                # There may not be an available project-local symbol or call edge.
                rule = 'import_points_to_project'
        elif len(parts) == 1 and head in dir(builtins) and not self._shadowed(call, head):
            category, rule, certainty = 'BUILTIN', 'python_builtin_unshadowed', 'high'
        elif len(parts) == 1:
            direct = self._same_module_def(call, head) if call else []
            if direct:
                category, rule, certainty = 'INTERNAL_CANDIDATE', 'same_module_symbol_not_resolved', 'medium'
                candidates = direct
            else:
                a = self._nearest_assignment(call, head) if call else None
                if a:
                    category, rule, certainty = 'INDIRECT_CALL', 'local_binding_callable_unknown', 'medium'
                    evid.update({'assignment_line': a.get('line'), 'assignment_type': a.get('type'),
                                 'binding': a.get('binding', {}).get('kind')})
                elif not in_module and head in self.functions.get(call.get('caller_id'), {}).get('parameters', []):
                    category, rule, certainty = 'INDIRECT_CALL', 'callable_parameter_unknown', 'high'
                elif in_module:
                    category, rule, certainty = 'MODULE_LEVEL', 'no_caller_module_node', 'high'
        elif callee.startswith(('self.', 'cls.')) and len(parts) > 2:
            category, rule, certainty = 'FIELD_CHAIN', 'chained_instance_field', 'high'
        elif len(parts) > 2 and reason == 'chained_attribute_not_supported':
            category, rule, certainty = 'FIELD_CHAIN', 'nested_attribute_access', 'high'
        elif len(parts) >= 2:
            # Literal collection methods: only when a simple local assignment
            # establishes the receiver's type. Not a sound flow proof.
            assignment = self._nearest_assignment(call, head) if call else None
            typename = _literal_type(assignment.get('value')) if assignment else None
            if len(parts) == 2 and typename and parts[1] in BASIC_METHODS.get(typename, set()):
                category, rule, certainty = 'BUILTIN_METHOD', 'simple_literal_receiver', 'medium'
                evid.update({'inferred_type': typename, 'assignment_line': assignment['line'],
                             'branch_depth': assignment.get('control_depth')})
            elif assignment and assignment.get('type') == 'FOR_TARGET':
                category, rule, certainty = 'INDIRECT_CALL', 'iteration_value_type_unknown', 'medium'
                evid.update({'assignment_line': assignment.get('line'), 'assignment_type': 'FOR_TARGET'})
            elif (len(parts) > 2 and head in {'self', 'cls'}) or reason == 'chained_attribute_not_supported':
                category, rule, certainty = 'FIELD_CHAIN', 'nested_attribute_access', 'high'
            else:
                category, rule, certainty = 'RECEIVER_UNKNOWN', 'receiver_type_not_proven', 'low'
        elif in_module:
            category, rule, certainty = 'MODULE_LEVEL', 'no_caller_module_node', 'high'

        # Module-level is a separate context; e.g. setuptools.setup() stays external.
        return {**item,
                'category': category, 'category_label': CATEGORIES[category],
                'scope_context': scope_context, 'classifier_rule': rule,
                'confidence': certainty, 'candidate_targets': candidates,
                'classification_evidence': evid}


def reclassify_graph(graph: dict, symbols: dict) -> dict:
    """Attach non-destructive labels to unresolved calls; leave graph edges intact."""
    classifier = Classifier(symbols)
    enriched = [classifier.classify(item) for item in graph.get('unresolved_calls', [])]
    counts = Counter(x['category'] for x in enriched)
    reasons = Counter(x['reason'] for x in enriched)
    contexts = Counter(x['scope_context'] for x in enriched)
    graph['unresolved_calls'] = enriched
    graph['classification_summary'] = {
        'total': len(enriched),
        'categories': {k: counts.get(k, 0) for k in CATEGORIES},
        'original_reasons': dict(sorted(reasons.items())),
        'scope_context': dict(sorted(contexts.items())),
        'category_labels': CATEGORIES.copy(),
        'warning': ('Classification is heuristic triage, not verified internal-call ground truth. '
                    'No external code was executed; confidence is not a probability.'),
    }
    assert sum(counts.values()) == graph['unresolved_call_sites']
    return graph


def write_classification(graph: dict, path: str | Path):
    """Human-review-friendly TSV-free CSV plus concise JSON; UTF-8 BOM for Excel."""
    from symbol_builder import save_json
    path = Path(path)
    path.mkdir(parents=True, exist_ok=True)
    save_json({'repository': graph.get('repository'),
               'summary': graph['classification_summary'],
               'calls': graph['unresolved_calls']}, path / 'unresolved_classification.json')
    fields = ['file', 'line', 'callee_text', 'caller_id', 'reason', 'category',
              'category_label', 'scope_context', 'classifier_rule', 'confidence',
              'candidate_targets', 'classification_evidence']
    with (path / 'unresolved_classification.csv').open('w', encoding='utf-8-sig', newline='') as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        for entry in graph['unresolved_calls']:
            writer.writerow({k: ', '.join(entry.get(k, [])) if k == 'candidate_targets' else
                             str(entry.get(k, '')) if k == 'classification_evidence' else
                             entry.get(k, '') for k in fields})
