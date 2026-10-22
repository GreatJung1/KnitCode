"""v2.4: structural and annotation-evidence consistency checks.

Usage:
  python verify_v24.py --graph output/graph_result.json --symbols output/symbol_result.json --repo SOURCE_ROOT
  python verify_v24.py --graph output/... --symbols output/... --old PREVIOUS_GRAPH_JSON

The optional --old summarizes differences; new edges are expected in v2.4.
These are NOT measurements of precision, recall, or actual runtime dispatch.
"""
from __future__ import annotations
import argparse
import json
from pathlib import Path
from verify_v23 import verify, verify_source, compare
from verify_v231 import check_status


def verify_hints(graph, symbols):
    rows = {row['call_id']: row for row in graph.get('call_statuses', [])}
    calls = {c['id']: c for c in symbols.get('calls', [])}
    annotation_edges = [e for e in graph.get('edges', []) if
                        e.get('type') == 'CALLS' and
                        (e.get('evidence') or {}).get('certainty') == 'declared_parameter_type_may_call']
    checks = [
        ('Every annotation edge cites a real AST call',
         all(e['evidence'].get('call_id') in calls for e in annotation_edges)),
        ('Annotation edges include an actual annotation binding',
         all(any(o.get('kind') == 'annotated_parameter'
                 for o in e['evidence'].get('reaching_definitions', []))
             for e in annotation_edges)),
        ('Annotation edges remain MAY_CALL and INTERNAL',
         all(rows.get(e['evidence'].get('call_id'), {}).get('certainty') == 'MAY_CALL' and
             rows.get(e['evidence'].get('call_id'), {}).get('status') == 'RESOLVED_INTERNAL'
             for e in annotation_edges)),
        ('All annotation edge targets are project methods',
         all(e['target'].startswith('METHOD:') for e in annotation_edges)),
    ]
    for label, ok in checks:
        print(f"[{'PASS' if ok else 'FAIL'}] {label}")
    print(f'Annotation-based CALLS edges: {len(annotation_edges)}, '
          f'unique call sites: {len({e["evidence"]["call_id"] for e in annotation_edges})}')
    print(f'v2.4 hint checks: PASS={sum(ok for _,ok in checks)} '
          f'FAIL={sum(not ok for _,ok in checks)}')
    return sum(not ok for _,ok in checks)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--graph', default='output/graph_result.json')
    parser.add_argument('--symbols', default='output/symbol_result.json')
    parser.add_argument('--repo', help='Source root for AST position verification')
    parser.add_argument('--old', help='Optional same-source v2.3.1 graph for change summary')
    args = parser.parse_args()
    graph = json.loads(Path(args.graph).read_text(encoding='utf-8'))
    symbols = json.loads(Path(args.symbols).read_text(encoding='utf-8'))
    err = verify(graph, symbols) + check_status(graph, symbols) + verify_hints(graph, symbols)
    if args.repo:
        err += verify_source(symbols, args.repo)
    if args.old:
        compare(json.loads(Path(args.old).read_text(encoding='utf-8')), graph)
    if err:
        raise SystemExit(1)
    print('v2.4 consistency checks passed. Not evidence of runtime accuracy.')


if __name__ == '__main__':
    main()
