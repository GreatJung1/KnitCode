"""KnitCode v2.2 graph verifier.

Compares v2.1 with v2.2; checks graph integrity and the 7 known Sublist3r
regression sites without executing the target project. Optional source-AST and
human-reviewed gold-label checks are separate from these regression assertions.

Usage:
    python verify_v22.py --old "<v2.1>/output/graph_result.json" \
        --new output/graph_result.json --repo "<Sublist3r repository>"
"""
from __future__ import annotations

import argparse
import ast
from collections import Counter
import json
from pathlib import Path
import sys
from typing import Any

ENUM_CALL = 'CALL:Sublist3r/sublist3r.py:943:13:287'
MODULE_CALLS = {
    'CALL:Sublist3r/subbrute/subbrute.py:591:17:186': ('MODULE:subbrute.subbrute', 'FUNCTION:subbrute.subbrute.extract_subdomains'),
    'CALL:Sublist3r/subbrute/subbrute.py:596:18:189': ('MODULE:subbrute.subbrute', 'FUNCTION:subbrute.subbrute.check_open'),
    'CALL:Sublist3r/subbrute/subbrute.py:605:12:191': ('MODULE:subbrute.subbrute', 'FUNCTION:subbrute.subbrute.error'),
    'CALL:Sublist3r/subbrute/subbrute.py:612:12:193': ('MODULE:subbrute.subbrute', 'FUNCTION:subbrute.subbrute.error'),
    'CALL:Sublist3r/subbrute/subbrute.py:633:12:198': ('MODULE:subbrute.subbrute', 'FUNCTION:subbrute.subbrute.print_target'),
    'CALL:Sublist3r/sublist3r.py:1006:4:312': ('MODULE:sublist3r', 'FUNCTION:sublist3r.interactive'),
}
ENGINE_NAMES = (
    'BaiduEnum', 'YahooEnum', 'GoogleEnum', 'BingEnum', 'AskEnum',
    'NetcraftEnum', 'DNSdumpster', 'Virustotal', 'ThreatCrowd',
    'CrtSearch', 'PassiveDNS',
)
ENGINE_CLASSES = {f'CLASS:sublist3r.{name}' for name in ENGINE_NAMES}
ENGINE_INITS = {f'METHOD:sublist3r.{name}.__init__' for name in ENGINE_NAMES}


class Audit:
    def __init__(self):
        self.passed = 0
        self.failures: list[str] = []
        self.notices: list[str] = []

    def check(self, ok: bool, message: str, detail: str = '') -> None:
        if ok:
            self.passed += 1
            print(f'[PASS] {message}')
        else:
            msg = message + (': ' + detail if detail else '')
            self.failures.append(msg)
            print(f'[FAIL] {msg}')

    def note(self, message: str) -> None:
        self.notices.append(message)
        print(f'[NOTE] {message}')


def load_json(path: Path) -> dict[str, Any]:
    result = json.loads(path.read_text(encoding='utf-8-sig'))
    if not isinstance(result, dict):
        raise ValueError(f'JSON root must be an object: {path}')
    return result


def edge_key(e: dict[str, Any]) -> tuple[str, str, str, str]:
    return (e.get('source', ''), e.get('target', ''), e.get('type', ''),
            (e.get('evidence') or {}).get('call_id', ''))


def site_edges(graph: dict[str, Any], call_id: str) -> list[dict[str, Any]]:
    return [e for e in graph.get('edges', []) if (e.get('evidence') or {}).get('call_id') == call_id]


def graph_sites(graph: dict[str, Any]) -> set[str]:
    return {e['evidence']['call_id'] for e in graph.get('edges', [])
            if e.get('type') in ('CALLS', 'INSTANTIATES') and e.get('evidence', {}).get('call_id')}


def integrity(audit: Audit, graph: dict[str, Any], label: str) -> None:
    nodes, edges, unresolved = (graph.get('nodes', []), graph.get('edges', []),
                                graph.get('unresolved_calls', []))
    node_ids = [n.get('id') for n in nodes]
    ids = set(node_ids)
    ek = [edge_key(e) for e in edges]
    resolved_ids = graph_sites(graph)
    unresolved_ids = [c.get('call_id') for c in unresolved]
    audit.check(len(node_ids) == len(ids) == graph.get('node_count'),
                f'{label}: unique node IDs / node_count')
    audit.check(len(ek) == len(set(ek)) == graph.get('edge_count'),
                f'{label}: unique edge keys / edge_count')
    audit.check(all(e.get('source') in ids and e.get('target') in ids for e in edges),
                f'{label}: no dangling edge endpoints')
    audit.check(len(resolved_ids) == graph.get('resolved_call_sites'),
                f'{label}: resolved_call_sites equals distinct resolved call IDs')
    audit.check(len(unresolved_ids) == len(set(unresolved_ids)) == graph.get('unresolved_call_sites'),
                f'{label}: unique unresolved IDs / unresolved_call_sites')
    audit.check(not (resolved_ids & set(unresolved_ids)),
                f'{label}: no simultaneously resolved/unresolved call IDs')
    audit.check(len(resolved_ids) + len(unresolved_ids) == graph.get('total_call_sites'),
                f'{label}: every site accounted for')


def regression(audit: Audit, before: dict[str, Any], after: dict[str, Any]) -> None:
    audit.check(before.get('repository') == after.get('repository'),
                'Same repository')
    audit.check(bool(before.get('commit')) and before.get('commit') == after.get('commit'),
                'Same pinned commit')
    audit.check(before.get('total_call_sites') == after.get('total_call_sites'),
                'No call sites lost/added during reanalysis')
    old_edges = {edge_key(e) for e in before['edges']}
    new_edges = {edge_key(e) for e in after['edges']}
    deleted = old_edges - new_edges
    added = new_edges - old_edges
    audit.check(not deleted, 'No preexisting edges deleted/retargeted',
                f'{len(deleted)} disappeared (examples: {sorted(deleted)[:2]})')
    audit.check(len(added) == 28, 'Exactly 28 added edges (6 module + 22 enum)',
                f'actual {len(added)}')
    old_unresolved = {c['call_id'] for c in before['unresolved_calls']}
    new_unresolved = {c['call_id'] for c in after['unresolved_calls']}
    expected = set(MODULE_CALLS) | {ENUM_CALL}
    audit.check(old_unresolved - new_unresolved == expected,
                'Exactly 7 expected call sites became resolved',
                f'actual {sorted(old_unresolved-new_unresolved)}')
    audit.check(not (new_unresolved-old_unresolved),
                'No newly unresolved sites',
                f'{len(new_unresolved-old_unresolved)} added')
    audit.check(after.get('node_count', 0) - before.get('node_count', 0) == 3,
                'Exactly three MODULE nodes added')
    added_site_ids = {k[3] for k in added}
    audit.check(added_site_ids == expected,
                'No unexpected call-site edges added',
                f'actual site IDs {sorted(added_site_ids)}')


def known_cases(audit: Audit, graph: dict[str, Any]) -> None:
    for cid, (source, target) in MODULE_CALLS.items():
        edges = site_edges(graph, cid)
        ok = (len(edges) == 1 and edges[0]['source'] == source and
              edges[0]['target'] == target and edges[0]['type'] == 'CALLS' and
              edges[0]['evidence'].get('module_level') is True)
        audit.check(ok, f'MODULE internal call: {cid.split(":")[-3]}:{cid.split(":")[-2]}',
                    f'expected {source} -> {target}')
    module_call_ids = {e['evidence']['call_id'] for e in graph['edges']
                       if e.get('type') == 'CALLS' and e.get('evidence', {}).get('module_level')}
    audit.check(module_call_ids == set(MODULE_CALLS),
                'No unexpected MODULE-level resolved calls')
    edges = site_edges(graph, ENUM_CALL)
    instances = {e['target'] for e in edges if e['type'] == 'INSTANTIATES'}
    inits = {e['target'] for e in edges if e['type'] == 'CALLS'}
    audit.check(instances == ENGINE_CLASSES,
                'enum(): exactly 11 expected class candidates',
                f'missing={sorted(ENGINE_CLASSES-instances)} extras={sorted(instances-ENGINE_CLASSES)}')
    audit.check(inits == ENGINE_INITS,
                'enum(): exactly 11 initializer targets',
                f'missing={sorted(ENGINE_INITS-inits)} extras={sorted(inits-ENGINE_INITS)}')
    audit.check(len(edges) == 22 and all(e['source'] == 'FUNCTION:sublist3r.main' for e in edges),
                'enum(): 22 edges from main()')
    audit.check(all(e['evidence'].get('certainty') == 'multiple_possible_targets' for e in edges),
                'enum(): all candidates marked MAY, not certain')
    ambiguous = [x for x in graph.get('ambiguous_calls', []) if x.get('call_id') == ENUM_CALL]
    audit.check(len(ambiguous) == 1 and set(ambiguous[0].get('targets', [])) == ENGINE_CLASSES,
                'enum(): ambiguity record matches 11 classes')
    audit.check(graph.get('module_level_resolved_sites') == 6 and
                graph.get('collection_indirect_resolved_sites') == 1,
                'Feature counters match six + one sites')


def source_ast_checks(audit: Audit, graph: dict[str, Any], repo: Path) -> None:
    if not repo.is_dir():
        audit.check(False, 'Source folder exists', str(repo))
        return
    expected = set(MODULE_CALLS) | {ENUM_CALL}
    node_ids = {n['id'] for n in graph['nodes']}
    by_file: dict[str, ast.AST] = {}
    node_by_id = {n['id']: n for n in graph['nodes']}
    for cid in sorted(expected):
        es = site_edges(graph, cid)
        if not es:
            audit.check(False, f'Source-AST: site exists: {cid}')
            continue
        ev = es[0]['evidence']
        file_name = ev['file']
        # The result uses Sublist3r/<relative path>, while --repo points at Sublist3r/.
        relative = Path(*Path(file_name).parts[1:]) if Path(file_name).parts[0] == 'Sublist3r' else Path(file_name)
        file_path = repo / relative
        if not file_path.is_file():
            audit.check(False, f'Source file for {cid}', str(file_path))
            continue
        if file_name not in by_file:
            by_file[file_name] = ast.parse(file_path.read_text(encoding='utf-8-sig'), filename=str(file_path))
        # Check the AST call name and whether the call is inside a function.
        found: list[tuple[ast.Call, int]] = []
        class Walker(ast.NodeVisitor):
            def __init__(self):
                self.function_depth = 0
            def visit_FunctionDef(self, n):
                self.function_depth += 1
                self.generic_visit(n)
                self.function_depth -= 1
            visit_AsyncFunctionDef = visit_FunctionDef
            def visit_Lambda(self, n):
                self.function_depth += 1
                self.generic_visit(n)
                self.function_depth -= 1
            def visit_Call(self, n):
                if n.lineno == ev['line'] and n.col_offset == ev['column']:
                    found.append((n, self.function_depth))
                self.generic_visit(n)
        Walker().visit(by_file[file_name])
        callname = 'enum' if cid == ENUM_CALL else node_by_id[MODULE_CALLS[cid][1]]['name']
        audit.check(len(found) == 1 and ast.unparse(found[0][0].func) == callname,
                    f'Source AST confirms call at {file_name}:{ev["line"]}:{ev["column"]}')
        if cid in MODULE_CALLS:
            target = MODULE_CALLS[cid][1]
            audit.check(bool(found) and found[0][1] == 0,
                        f'MODULE call really outside functions: {cid}')
            matching_defs = [n for n in ast.walk(by_file[file_name])
                             if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
                             and n.name == node_by_id[target]['name']
                             and n.lineno == node_by_id[target]['line']]
            audit.check(target in node_ids and len(matching_defs) == 1,
                        f'MODULE target definition is real: {cid}')
        else:
            audit.check(bool(found) and found[0][1] > 0,
                        'enum() really called inside main-like function')
    enum_class_defs = {f'CLASS:sublist3r.{n.name}': n.lineno
                       for n in ast.walk(by_file.get('Sublist3r/sublist3r.py', ast.Module(body=[], type_ignores=[])))
                       if isinstance(n, ast.ClassDef)}
    audit.check(all(enum_class_defs.get(c) == node_by_id[c]['line'] for c in ENGINE_CLASSES),
                'All 11 enum classes match actual source AST definitions')


def score_reviewed_gold(audit: Audit, graph: dict[str, Any], path: Path) -> None:
    gold = load_json(path)
    rows = gold.get('sites', [])
    reviewed = [x for x in rows if x.get('reviewed') is True]
    audit.check(isinstance(rows, list) and all(
        isinstance(x, dict) and isinstance(x.get('call_id'), str) and
        x.get('edge_type') in ('CALLS', 'INSTANTIATES') and
        isinstance(x.get('targets'), list) and
        all(isinstance(t, str) for t in x.get('targets', [])) and
        isinstance(x.get('reviewed'), bool)
        for x in rows), 'Gold file format is valid')
    if not reviewed:
        audit.note('Gold labels: 0 reviewed cases; Precision/Recall NOT calculated.')
        return
    tp = fp = fn = 0
    for case in reviewed:
        cid, edge_type = case['call_id'], case['edge_type']
        expected = set(case['targets'])
        predicted = {e['target'] for e in site_edges(graph, cid) if e['type'] == edge_type}
        tp += len(predicted & expected)
        fp += len(predicted - expected)
        fn += len(expected - predicted)
        audit.check(expected == predicted, f'Human-reviewed targets: {cid}',
                    f'FP={sorted(predicted-expected)}, FN={sorted(expected-predicted)}')
    precision = tp/(tp+fp) if tp+fp else None
    recall = tp/(tp+fn) if tp+fn else None
    f1 = 2*precision*recall/(precision+recall) if precision is not None and recall is not None and precision+recall else None
    fmt = lambda n: 'N/A' if n is None else f'{n:.3f}'
    audit.note(f'LABELLED-SUBSET ONLY: {len(reviewed)} sites, TP={tp}, FP={fp}, FN={fn}, '
               f'Precision={fmt(precision)}, Recall={fmt(recall)}, F1={fmt(f1)}')
    audit.note('These are NOT project-wide Precision/Recall values.')


def main() -> int:
    p = argparse.ArgumentParser(description='Verify KnitCode v2.2 graph regression and reference cases')
    p.add_argument('--old', type=Path, help='v2.1/output/graph_result.json (regression comparison)')
    p.add_argument('--new', type=Path, default=Path('output/graph_result.json'),
                   help='v2.2/output/graph_result.json (default: output/graph_result.json)')
    p.add_argument('--repo', type=Path, help='Optional: original Sublist3r source root for AST checks')
    p.add_argument('--gold', type=Path, help='Optional: manually reviewed target labels JSON')
    args = p.parse_args()
    a = Audit()
    try:
        new = load_json(args.new)
        integrity(a, new, 'v2.2')
        known_cases(a, new)
        if args.old:
            old = load_json(args.old)
            integrity(a, old, 'v2.1')
            regression(a, old, new)
        else:
            a.note('No --old: regression comparison skipped.')
        if args.repo:
            source_ast_checks(a, new, args.repo)
        if args.gold:
            score_reviewed_gold(a, new, args.gold)
    except (OSError, ValueError, KeyError, TypeError, SyntaxError) as exc:
        a.check(False, 'Verifier could not finish', f'{type(exc).__name__}: {exc}')
    print('\n' + '='*65)
    print(f'PASS={a.passed}  FAIL={len(a.failures)}')
    if a.failures:
        print('Failures:')
        for f in a.failures:
            print(f'  - {f}')
    else:
        print('Regression/reference checks passed. This is NOT a full accuracy benchmark.')
    return 1 if a.failures else 0


if __name__ == '__main__':
    sys.exit(main())
