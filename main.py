"""Usage: python main.py Sublist3r [--output-dir output]"""
import argparse
import subprocess
from pathlib import Path
from ast_parser import scan_repository
from symbol_builder import build_symbol_result, save_json
from graph_builder import build_graph
from graph_report import write_html
from unresolved_classifier import reclassify_graph, write_classification
from call_status import annotate_call_statuses, write_call_statuses
from dependency_analysis import analyze_dependencies, write_dependency_results


def run(repo: str | Path, output_dir: str | Path = 'output', *,
        analyze_deps: bool = False, dep_roots=(), max_dep_files: int = 300):
    ast_data = scan_repository(repo)
    try:
        head = subprocess.run(
            ['git', '-C', str(Path(repo).resolve()), 'rev-parse', 'HEAD'],
            capture_output=True, text=True, check=True).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        head = None
    ast_data['commit'] = head
    symbols = build_symbol_result(ast_data)
    symbols['commit'] = head
    graph = annotate_call_statuses(reclassify_graph(build_graph(symbols), symbols), symbols)
    graph['commit'] = head
    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)
    save_json(ast_data, out / 'ast_result.json')
    save_json(symbols, out / 'symbol_result.json')
    save_json(graph, out / 'graph_result.json')
    write_classification(graph, out)
    write_call_statuses(graph, out)
    write_html(graph, out / 'report.html')
    if analyze_deps:
        dep_result = analyze_dependencies(symbols, graph, roots=dep_roots, max_files=max_dep_files)
        write_dependency_results(dep_result, out)
        print('Dependency source analysis:', dep_result['summary'])
    print('=== KnitCode static call graph v2.4 (parameter type hints) ===')
    print(f"Commit          : {head or 'not a git repo'}")
    print(f"Python files    : {ast_data['python_file_count']}")
    print(f"Parse errors    : {len(ast_data['parse_errors'])}")
    for err in ast_data['parse_errors']:
        print(f"  {err['file']}: {err['message']}")
    print(f"Symbols         : {len(symbols['symbols'])}")
    print(f"Call sites      : {graph['total_call_sites']}")
    print(f"Resolved sites  : {graph['resolved_call_sites']}")
    print(f"Unresolved sites: {graph['unresolved_call_sites']}")
    print(f"Module-level resolved sites: {graph['module_level_resolved_sites']}")
    print(f"Collection indirect sites   : {graph['collection_indirect_resolved_sites']}")
    print(f"Graph edges     : {graph['edge_count']} (CONTAINS/INHERITS/CALLS/INSTANTIATES)")
    print(f"Ambiguous sites : {len(graph['ambiguous_calls'])} (multiple may-call candidates)")
    print(f"Conditional     : {len(graph['conditional_calls'])} (may be unbound)")
    print("Call-site status:")
    for status, count in graph['status_summary']['statuses'].items():
        print(f"  {status:20}: {count}")
    print("Known non-internal origin:")
    for kind, count in graph['status_summary']['known_non_internal_origins'].items():
        print(f"  {kind:20}: {count}")
    print("Classification :")
    for category, count in graph["classification_summary"]["categories"].items():
        if count:
            print(f"  {category:20}: {count}")
    print("  Note: categories are heuristic, not verified ground truth.")
    print(f"Output          : {out.resolve()} (report.html, classification JSON/CSV, call_statuses JSON/CSV)")
    return ast_data, symbols, graph


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='Analyze a Python repo statically (never execute target code)')
    parser.add_argument('repo', help='Directory of Python repository (e.g. ./Sublist3r)')
    parser.add_argument('--output-dir', default='output')
    parser.add_argument('--analyze-deps', action='store_true',
                        help='Inspect stdlib and installed/external imported source using AST only')
    parser.add_argument('--dep-root', action='append', default=[],
                        help='Extra dependency source root (site-packages or one package); repeatable')
    parser.add_argument('--max-dep-files', type=int, default=300,
                        help='Bound inspected dependency Python source files (default 300)')
    args = parser.parse_args()
    run(args.repo, args.output_dir, analyze_deps=args.analyze_deps,
        dep_roots=args.dep_root, max_dep_files=max(1, args.max_dep_files))
