"""Reproducible diagnostics; analyzes fixture source, never executes it."""
import argparse
import gc
import hashlib
import json
from pathlib import Path
import statistics
import sys
import time
import tracemalloc


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--engine', type=Path, required=True)
    parser.add_argument('--cases', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    sys.path.insert(0, str(args.engine.resolve()))
    # When this script resides in the new engine, put the selected engine first.
    from ast_parser import scan_repository
    from symbol_builder import build_symbol_result
    from graph_builder import Resolver
    from unresolved_classifier import reclassify_graph
    from call_status import annotate_call_statuses
    paths = sorted(p for p in args.cases.iterdir() if (p / 'gold.json').exists())
    elapsed, peaks = [], []
    summaries, stats, metrics = {}, {}, {}
    schemas = {}
    for repeat in range(5):
        gc.collect()
        tracemalloc.start()
        start = time.perf_counter()
        tp = fp = fn = passed = 0
        for path in paths:
            data = build_symbol_result(scan_repository(path))
            resolver = Resolver(data)
            graph = annotate_call_statuses(reclassify_graph(resolver.build(), data), data)
            gold = json.loads((path / 'gold.json').read_text(encoding='utf-8'))
            rows = [r for r in graph['call_statuses'] if r['callee_text'] == gold['focus_callee']]
            actual = set(rows[0]['target_ids']) if len(rows) == 1 else set()
            expected = set(gold['expected_targets'])
            tp += len(actual & expected); fp += len(actual - expected); fn += len(expected - actual)
            passed += actual == expected
            if repeat == 0:
                schemas[path.name] = {'graph_keys': sorted(graph), 'status_keys': sorted(rows[0]) if rows else []}
                stats[path.name] = getattr(resolver, 'object_flow_stats', {})
                summaries[path.name] = {key: {'alternatives': [vars(a) for a in sorted(value.atoms, key=lambda a: (a.kind, a.name, a.site))],
                                                    'provenance': sorted(value.tags)}
                                       for key, value in getattr(resolver, 'return_summaries', {}).items()}
        elapsed.append(time.perf_counter() - start)
        peaks.append(tracemalloc.get_traced_memory()[1])
        tracemalloc.stop()
        metrics = {'TP': tp, 'FP': fp, 'FN': fn, 'precision': tp / (tp + fp) if tp + fp else None,
                   'recall': tp / (tp + fn) if tp + fn else None, 'target_set_matches': passed,
                   'note': 'Only gold focus target IDs; caller/scope correctness is measured separately by the original runner.'}
    output = {'python': sys.version, 'engine': str(args.engine.resolve()), 'cases': len(paths),
              'seconds_with_tracemalloc': elapsed, 'median_seconds': statistics.median(elapsed),
              'peak_traced_bytes': peaks, 'median_peak_bytes': statistics.median(peaks),
              'metrics': metrics, 'schemas': schemas, 'analysis_stats': stats, 'return_summaries': summaries,
              'input_sha256': {str(p.relative_to(args.cases)): hashlib.sha256(p.read_bytes()).hexdigest()
                               for p in args.cases.rglob('*') if p.suffix in ('.py', '.json')}}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(output, indent=2, ensure_ascii=False), encoding='utf-8')
    print(json.dumps({k: output[k] for k in ('engine', 'cases', 'median_seconds', 'median_peak_bytes', 'metrics')}, indent=2))


if __name__ == '__main__':
    main()
