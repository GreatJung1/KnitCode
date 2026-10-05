"""Compare two graph_result.json files from the same pinned repository."""
from __future__ import annotations
import argparse
import json
from pathlib import Path


def compare(old: dict, new: dict) -> dict:
    if old.get('repository') != new.get('repository'):
        raise ValueError('The repository names differ; compare the same repository.')
    a, b = old.get('commit'), new.get('commit')
    if a and b and a != b:
        raise ValueError('Different commits; pin Sublist3r to the same SHA before comparison.')
    def sites(graph):
        result = {}
        for edge in graph.get('edges', []):
            if edge.get('type') not in ('CALLS', 'INSTANTIATES'):
                continue
            item = edge.get('evidence') or {}
            site = item.get('call_id')
            if site:
                result.setdefault(site, {'source': item.get('source'), 'file': item.get('file'),
                                         'line': item.get('line'), 'targets': set()})['targets'].add(edge.get('target'))
        return result
    before, after = sites(old), sites(new)
    new_sites = after.keys() - before.keys()
    lost_sites = before.keys() - after.keys()
    changed_sites = {x for x in (before.keys() & after.keys()) if before[x]['targets'] != after[x]['targets']}
    def detail(ids, lookup):
        return [{'call_id': key, **{k: sorted(v) if isinstance(v, set) else v
                                    for k, v in lookup[key].items()}} for key in sorted(ids)]
    return {'old_resolved': len(before), 'new_resolved': len(after),
            'newly_resolved': detail(new_sites, after),
            'lost_resolutions': detail(lost_sites, before),
            'changed_targets': [{'call_id': key, 'before': sorted(before[key]['targets']),
                                 'after': sorted(after[key]['targets'])} for key in sorted(changed_sites)]}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('v1', type=Path, help='v1 output/graph_result.json')
    parser.add_argument('v2', type=Path, help='v2 output/graph_result.json')
    args = parser.parse_args()
    result = compare(json.loads(args.v1.read_text(encoding='utf-8')),
                     json.loads(args.v2.read_text(encoding='utf-8')))
    print(json.dumps(result, ensure_ascii=False, indent=2))

if __name__ == '__main__':
    main()
