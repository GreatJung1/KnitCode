"""Use existing v2.3 output; no need to re-scan the repository.

Usage: python reclassify_existing.py "C:/.../knitcode_sublist3r_v2/output"
"""
import argparse
from pathlib import Path
from graph_report import write_html
from symbol_builder import load_json, save_json
from unresolved_classifier import reclassify_graph, write_classification
from call_status import annotate_call_statuses, write_call_statuses


def run(output_dir='output'):
    root = Path(output_dir)
    symbols = load_json(root / 'symbol_result.json')
    graph = load_json(root / 'graph_result.json')
    result = annotate_call_statuses(reclassify_graph(graph, symbols), symbols)
    save_json(result, root / 'graph_result.json')
    write_classification(result, root)
    write_call_statuses(result, root)
    write_html(result, root / 'report.html')
    for category, count in result['classification_summary']['categories'].items():
        if count:
            print(f'{category:20}: {count}')
    print(f"총 재분류: {result['classification_summary']['total']}개; 호출 관계는 변경하지 않음")
    print('호출 상태:', result['status_summary']['statuses'])
    print('보고서:', (root / 'report.html').resolve())
    return result

if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='Classify v2 unresolved calls without executing target code')
    parser.add_argument('output_dir', nargs='?', default='output')
    args = parser.parse_args()
    run(args.output_dir)
