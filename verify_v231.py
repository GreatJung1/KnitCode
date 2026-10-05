"""v2.3.1 verification: status partition + existing v2.3 structural checks.

Usage:
  python verify_v231.py --graph output/graph_result.json --symbols output/symbol_result.json
  python verify_v231.py --graph ... --symbols ... --repo SOURCE_ROOT --old V23_GRAPH.json
These are consistency checks, NOT precision/recall or runtime call-truth tests.
"""
import argparse
from collections import Counter
import json
from pathlib import Path

from verify_v23 import verify, verify_source, edge_key
from call_status import STATES, NON_INTERNAL


def check_status(graph, symbols):
    calls = symbols.get('calls', [])
    rows = graph.get('call_statuses', [])
    uncalls = {c['call_id'] for c in graph.get('unresolved_calls', [])}
    resolved = {e.get('evidence', {}).get('call_id') for e in graph.get('edges', [])
                if e.get('evidence', {}).get('call_id')}
    counts = Counter(r.get('status') for r in rows)
    origins = Counter(r.get('origin_kind') for r in rows if r.get('status') == 'KNOWN_NON_INTERNAL')
    summary = graph.get('status_summary', {})
    checks = [
        ('Every AST call has one status', len(rows) == len(calls) == len({r['call_id'] for r in rows})
         and {r['call_id'] for r in rows} == {c['id'] for c in calls}),
        ('All statuses use defined names', {r.get('status') for r in rows}.issubset(set(STATES))),
        ('Status summary totals match', summary.get('total') == len(rows) and
         summary.get('statuses') == {s: counts[s] for s in STATES}),
        ('Internal label follows resolved graph sites',
         {r['call_id'] for r in rows if r['status'] == 'RESOLVED_INTERNAL'} == resolved),
        ('Non-internal and unknown partition unresolved sites',
         {r['call_id'] for r in rows if r['status'] != 'RESOLVED_INTERNAL'} == uncalls),
        ('Non-internal originates from builtin or import evidence only',
         all(r['origin_kind'] in NON_INTERNAL.values() and not r['target_ids'] and
             r['evidence'].get('rule') in {'UNSHADOWED_IMPORT_ORIGIN', 'UNSHADOWED_BUILTIN_NAME'}
             for r in rows if r['status'] == 'KNOWN_NON_INTERNAL')),
        ('Origin counters correct', summary.get('known_non_internal_origins') ==
         {name: origins[name] for name in ('BUILTIN', 'STDLIB', 'EXTERNAL_IMPORT')}),
        ('Unknown entries not stated as verified',
         all(r['certainty'] == 'UNVERIFIED' and not r['target_ids']
             for r in rows if r['status'] == 'UNKNOWN')),
        ('Internal targets correspond to graph',
         all(set(r['target_ids']) == {e['target'] for e in graph.get('edges', [])
                                     if e.get('evidence', {}).get('call_id') == r['call_id']}
             for r in rows if r['status'] == 'RESOLVED_INTERNAL')),
        ('Call evidence and source locations are present',
         all(r.get('evidence') and r.get('file') and r.get('line') is not None for r in rows)),
    ]
    for name, ok in checks:
        print(f"[{'PASS' if ok else 'FAIL'}] {name}")
    print(f"Call status: PASS={sum(ok for _, ok in checks)} FAIL={sum(not ok for _, ok in checks)}")
    return sum(not ok for _, ok in checks)


def check_backward(old, new):
    keys = ('node_count', 'edge_count', 'total_call_sites', 'resolved_call_sites',
            'unresolved_call_sites', 'module_level_resolved_sites',
            'collection_indirect_resolved_sites')
    tests = [
        ('v2.3 counters unchanged', all(old.get(k) == new.get(k) for k in keys)),
        ('v2.3 nodes unchanged', old.get('nodes') == new.get('nodes')),
        ('v2.3 graph edges unchanged', {edge_key(e) for e in old.get('edges', [])} ==
         {edge_key(e) for e in new.get('edges', [])}),
        ('v2.3 unresolved reasons and categories unchanged',
         {(x['call_id'], x.get('reason'), x.get('category')) for x in old.get('unresolved_calls', [])} ==
         {(x['call_id'], x.get('reason'), x.get('category')) for x in new.get('unresolved_calls', [])}),
    ]
    for name, ok in tests:
        print(f"[{'PASS' if ok else 'FAIL'}] {name}")
    return sum(not ok for _, ok in tests)


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--graph', default='output/graph_result.json')
    p.add_argument('--symbols', default='output/symbol_result.json')
    p.add_argument('--repo', default=None)
    p.add_argument('--old', default=None, help='Optional graph_result.json from v2.3 SAME source snapshot')
    a = p.parse_args()
    graph = json.loads(Path(a.graph).read_text(encoding='utf-8'))
    symbols = json.loads(Path(a.symbols).read_text(encoding='utf-8'))
    errs = verify(graph, symbols) + check_status(graph, symbols)
    if a.repo:
        errs += verify_source(symbols, a.repo)
    if a.old:
        errs += check_backward(json.loads(Path(a.old).read_text(encoding='utf-8')), graph)
    if errs:
        raise SystemExit(1)
    print('All requested consistency checks passed. NOT an accuracy/recall benchmark.')


if __name__ == '__main__':
    main()
