"""Generic static-graph structural verification, with optional version diff.

This is NOT a precision/recall or source-ground-truth accuracy benchmark.
Usage:
  python verify_v23.py --graph output/graph_result.json --symbols output/symbol_result.json
  python verify_v23.py --graph output/graph_result.json --symbols output/symbol_result.json --old old_v22_graph_result.json
"""
from __future__ import annotations

import argparse
import ast
from collections import Counter
from pathlib import Path
import json
import sys


def edge_key(e):
    return (e['source'], e['target'], e['type'], e.get('evidence', {}).get('call_id'))


def verify(graph: dict, symbols: dict) -> int:
    checks = []
    def check(name, assertion):
        checks.append((name, bool(assertion)))
    syms = symbols.get('symbols', [])
    nodes, edges = graph.get('nodes', []), graph.get('edges', [])
    calls = symbols.get('calls', [])
    unresolved = graph.get('unresolved_calls', [])
    nids = [n['id'] for n in nodes]
    sids = [s['id'] for s in syms]
    call_ids = [c['id'] for c in calls]
    ucids = [u['call_id'] for u in unresolved]
    edkeys = [edge_key(e) for e in edges]
    resolved_ids = {e.get('evidence', {}).get('call_id') for e in edges if e.get('evidence', {}).get('call_id')}
    all_call_ids = set(call_ids)
    u_set = set(ucids)
    n_set = set(nids)
    check('Unique symbol IDs and symbol_count', len(sids) == len(set(sids)) == symbols.get('symbol_count'))
    check('Unique graph node IDs and node_count', len(nids) == len(n_set) == graph.get('node_count'))
    check('Unique graph edge relations and edge_count', len(edkeys) == len(set(edkeys)) == graph.get('edge_count'))
    check('No dangling edge endpoints', all(e['source'] in n_set and e['target'] in n_set for e in edges))
    check('All declared symbols exist as graph nodes', set(sids).issubset(n_set))
    check('Unique AST call IDs and total_call_sites', len(call_ids) == len(all_call_ids) == graph.get('total_call_sites'))
    check('All edges cite known call sites or structural facts', resolved_ids.issubset(all_call_ids) and
          all(not e.get('evidence', {}).get('call_id') or e['type'] in {'CALLS', 'INSTANTIATES'} for e in edges))
    check('Unique unresolved IDs and unresolved_call_sites', len(ucids) == len(u_set) == graph.get('unresolved_call_sites'))
    check('Resolved count agrees with distinct edge call IDs', len(resolved_ids) == graph.get('resolved_call_sites'))
    check('No simultaneously resolved and unresolved sites', not (resolved_ids & u_set))
    check('All call sites accounted for', resolved_ids | u_set == all_call_ids)
    modules = {n['id'] for n in nodes if n['kind'] == 'MODULE'}
    mod_call_ids = {e['evidence']['call_id'] for e in edges if e.get('evidence', {}).get('module_level') and
                    e['evidence'].get('call_id')}
    check('Module-level callers are real MODULE nodes',
          all(e['source'] in modules for e in edges if e.get('evidence', {}).get('module_level')))
    check('Module-level resolved-site counter', len(mod_call_ids) == graph.get('module_level_resolved_sites'))
    check('Unresolved classification totals',
          sum(graph.get('classification_summary', {}).get('categories', {}).values()) == len(unresolved))
    check('Ambiguity records refer to actual resolved calls',
          all(x['call_id'] in resolved_ids and len(x.get('targets', [])) > 1 and
              all(any(e['target'] == t and e.get('evidence', {}).get('call_id') == x['call_id'] for e in edges)
                  for t in x['targets']) for x in graph.get('ambiguous_calls', [])))
    check('Overload metadata not duplicated in graph',
          all(not any(s.get('canonical_name') == d.get('canonical_name') and
                      s.get('line') in d.get('lines', []) for s in syms)
                  for d in symbols.get('overload_declarations', []) if d.get('has_implementation')))
    for name, value in checks:
        print(f"[{'PASS' if value else 'FAIL'}] {name}")
    print(f"PASS={sum(v for _,v in checks)} FAIL={sum(not v for _,v in checks)}")
    print('Static integrity checks only: NOT an accuracy/precision benchmark.')
    return sum(not v for _,v in checks)


def compare(old: dict, graph: dict) -> int:
    if old.get('repository') != graph.get('repository'):
        print('[WARN] Different repository labels; comparing call IDs may be misleading.')
    old_u = {x['call_id'] for x in old.get('unresolved_calls', [])}
    new_u = {x['call_id'] for x in graph.get('unresolved_calls', [])}
    old_r = {e['evidence']['call_id'] for e in old.get('edges', []) if e.get('evidence', {}).get('call_id')}
    new_r = {e['evidence']['call_id'] for e in graph.get('edges', []) if e.get('evidence', {}).get('call_id')}
    old_sites, new_sites = old_u | old_r, new_u | new_r
    old_edges = {edge_key(x) for x in old.get('edges', [])}
    new_edges = {edge_key(x) for x in graph.get('edges', [])}
    changes = {
        'Lost call IDs': old_sites-new_sites,
        'Added call IDs': new_sites-old_sites,
        'Previously resolved -> unresolved': old_r & new_u,
        'Previously unresolved -> resolved': old_u & new_r,
        'Removed graph relations': old_edges-new_edges,
        'Added graph relations': new_edges-old_edges,
    }
    print('\n--- Old/new comparison (review changes, not accuracy scoring) ---')
    for label, values in changes.items():
        print(f'{label}: {len(values)}')
        for item in sorted(values, key=str)[:5]:
            print('  ',item)
    if old_sites != new_sites:
        print('[WARN] AST site sets differ; check repo commit, analyzed root and analyzer extraction rules.')
    if not old.get('commit') or not graph.get('commit'):
        print('[WARN] Pinned commit cannot be checked for at least one analysis.')
    elif old['commit'] != graph['commit']:
        print('[WARN] Source commits differ.')
    return 0


def verify_source(symbols: dict, repo: str | Path) -> int:
    """Cross-check each emitted call/symbol line against actual Python AST.

    The original project code is *parsed only*, never imported/executed.
    Validating existence is not proving target resolution correctness.
    """
    repo = Path(repo).resolve()
    prefix = repo.name + '/'
    calls, definitions, skipped = set(), set(), set()
    file_names = {x['file'] for x in symbols.get('symbols', [])}
    file_names |= {x['file'] for x in symbols.get('calls', [])}
    for name in sorted(file_names):
        rel = name.replace('\\', '/')
        if not rel.startswith(prefix):
            print(f'[FAIL] Source-root mismatch: {rel}, expected prefix {prefix!r}')
            return 1
        path = repo / rel[len(prefix):]
        if not path.is_file():
            print(f'[FAIL] Missing source file: {path}')
            return 1
        try:
            tree = ast.parse(path.read_text(encoding='utf-8-sig'), filename=str(path))
        except (OSError, UnicodeError, SyntaxError) as exc:
            print(f'[FAIL] Source no longer parses: {path}: {exc}')
            return 1
        for n in ast.walk(tree):
            where = (rel, getattr(n, 'lineno', None), getattr(n, 'col_offset', None))
            if isinstance(n, ast.Call):
                calls.add(where)
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                definitions.add(where)
    missing_calls = [c for c in symbols.get('calls', [])
                     if (c['file'], c.get('line'), c.get('column')) not in calls]
    missing_defs = [s for s in symbols.get('symbols', [])
                    if (s['file'], s.get('line'), s.get('column')) not in definitions]
    valid = not (missing_calls or missing_defs)
    print(f"[{'PASS' if valid else 'FAIL'}] AST source locations: "
          f"{len(symbols.get('calls', [])) - len(missing_calls)} calls, "
          f"{len(symbols.get('symbols', [])) - len(missing_defs)} symbols verified")
    for item in (missing_calls + missing_defs)[:8]:
        print('   Missing AST location:', item['file'], item.get('line'), item.get('column'))
    print('Source-location evidence only: NOT proof of true call targets.')
    return int(not valid)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--graph', default='output/graph_result.json')
    parser.add_argument('--symbols', default='output/symbol_result.json')
    parser.add_argument('--old', default=None, help='Optional v2.2/earlier graph_result.json')
    parser.add_argument('--repo', default=None, help='Optional original source root for AST location validation')
    args = parser.parse_args()
    try:
        graph = json.loads(Path(args.graph).read_text(encoding='utf-8'))
        symbols = json.loads(Path(args.symbols).read_text(encoding='utf-8'))
        failures = verify(graph, symbols)
        if args.repo:
            failures += verify_source(symbols, args.repo)
        if args.old:
            compare(json.loads(Path(args.old).read_text(encoding='utf-8')), graph)
        sys.exit(1 if failures else 0)
    except (OSError, ValueError, KeyError, TypeError) as exc:
        print('Validation error:', exc, file=sys.stderr)
        sys.exit(2)


if __name__=='__main__':
    main()
