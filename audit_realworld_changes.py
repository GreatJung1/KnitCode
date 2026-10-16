"""Audit call-site deltas from two pinned KnitCode graph JSONs.

This is a static, reproducible *review queue* generator, not a TP/FP oracle.
It never imports or executes the target repository. Only uses stored graph facts
and, optionally, reads source text in a provided repository folder.
"""
from __future__ import annotations

import argparse
import ast
import csv
import hashlib
import json
from collections import Counter, defaultdict, deque
from pathlib import Path


def load(path):
    return json.loads(Path(path).read_text(encoding='utf-8-sig'))


def call_sites(graph):
    indexed = {}
    for edge in graph.get('edges', []):
        if edge.get('type') not in ('CALLS', 'INSTANTIATES'):
            continue
        evidence = edge.get('evidence') or {}
        cid = evidence.get('call_id')
        if not cid:
            continue
        item = indexed.setdefault(cid, {'targets': set(), 'relations': set(),
                                        'rules': set(), 'source': evidence.get('source'),
                                        'file': evidence.get('file'), 'line': evidence.get('line'),
                                        'caller': edge.get('source'), 'evidence': []})
        item['targets'].add(edge['target'])
        item['relations'].add(edge['type'])
        item['rules'].add(evidence.get('rule', ''))
        item['evidence'].append({'relation': edge['type'], 'target': edge['target'],
                                 'rule': evidence.get('rule'), 'certainty': evidence.get('certainty')})
    return indexed


def statuses(graph):
    return {x['call_id']: x for x in graph.get('call_statuses', [])}


def normalized(target, renames):
    if ':' not in target:
        return target
    kind, canonical = target.split(':', 1)
    for old, new in renames:
        if canonical == old or canonical.startswith(old + '.'):
            return kind + ':' + new + canonical[len(old):]
    return target


def source_group(file):
    parts = Path((file or '').replace('\\', '/')).parts
    if any(p in ('tests', 'test', 'testing') or p.startswith('test_') for p in parts):
        return 'test'
    if any(p in ('examples', 'example', 'docs', 'doc', 'benchmarks') for p in parts):
        return 'example'
    return 'production'


def read_excerpt(repo_dir, stored_file, center, radius=2):
    if repo_dir is None or not stored_file or not center:
        return ''
    parts = Path(stored_file.replace('\\', '/')).parts
    if parts and parts[0].lower() == repo_dir.name.lower():
        parts = parts[1:]
    try:
        path = repo_dir.joinpath(*parts).resolve()
        if not path.is_relative_to(repo_dir.resolve()) or not path.is_file():
            return ''
        lines = path.read_text(encoding='utf-8-sig').splitlines()
        return '\n'.join(f'{i + 1}: {lines[i]}' for i in range(max(0, center - radius - 1),
                                        min(len(lines), center + radius)))
    except (OSError, UnicodeError, ValueError, TypeError):
        return ''


def relevant_imports(repo_dir, stored_file, call_source):
    if repo_dir is None or not stored_file:
        return ''
    parts = Path(stored_file.replace('\\', '/')).parts
    if parts and parts[0].lower() == repo_dir.name.lower():
        parts = parts[1:]
    try:
        path = repo_dir.joinpath(*parts).resolve()
        if not path.is_relative_to(repo_dir.resolve()):
            return ''
        tree = ast.parse(path.read_text(encoding='utf-8-sig'))
        root = (call_source or '').split('(')[0].strip().split('.')[0]
        entries = []
        for node in ast.walk(tree):
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                names = [x.asname or x.name.split('.')[0] for x in node.names]
                if root in names:
                    entries.append(f'{node.lineno}: {ast.unparse(node)}')
        return '\n'.join(entries[:20])
    except (OSError, UnicodeError, ValueError, SyntaxError):
        return ''


def analyze(before, after, *, renames=(), repo_dir=None, sample_size=50):
    if before.get('repository') != after.get('repository'):
        raise ValueError('Repository mismatch')
    if before.get('commit') != after.get('commit'):
        raise ValueError('Commit mismatch (both must be identical, including null)')
    old_status, new_status = statuses(before), statuses(after)
    if old_status and new_status:
        old_call_ids, new_call_ids = set(old_status), set(new_status)
        if old_call_ids != new_call_ids:
            raise ValueError('Different call-site sets; same input is required')
        mismatches = [k for k in old_call_ids
                      if (old_status[k].get('file'), old_status[k].get('line'), old_status[k].get('callee_text'))
                      != (new_status[k].get('file'), new_status[k].get('line'), new_status[k].get('callee_text'))]
        if mismatches:
            raise ValueError(f'Call-site source facts changed: {len(mismatches)}')
    old, new = call_sites(before), call_sites(after)
    new_nodes = {n['id']: n for n in after.get('nodes', [])}
    added = sorted(set(new) - set(old))
    removed = sorted(set(old) - set(new))
    changed = sorted(k for k in set(old) & set(new) if old[k]['targets'] != new[k]['targets'])
    renames_only = []
    substantive = []
    for cid in changed:
        old_targets = {normalized(t, renames) for t in old[cid]['targets']}
        if old_targets == new[cid]['targets']:
            renames_only.append(cid)
        else:
            substantive.append(cid)

    def row(cid, label, src):
        info = src[cid]
        status_a = old_status.get(cid, {})
        status_b = new_status.get(cid, {})
        ids = sorted(info['targets'])
        # Existence in the graph's own node table is a consistency check,
        # not proof of reachability or correct runtime dispatch.
        all_nodes_exist = all(t in new_nodes for t in ids)
        return {'change': label, 'call_id': cid, 'file': info['file'], 'line': info['line'],
                'source': info['source'], 'caller': info['caller'],
                'group': source_group(info['file']),
                'old_status': status_a.get('status', ''),
                'old_origin': status_a.get('origin_kind', ''),
                'new_status': status_b.get('status', ''),
                'old_targets': sorted(old.get(cid, {}).get('targets', [])),
                'new_targets': ids, 'relations': sorted(info['relations']),
                'rules': sorted(info['rules']), 'all_target_nodes_exist': all_nodes_exist,
                'target_definition_locations': [{"target": t, "file": new_nodes.get(t, {}).get('file'),
                                                 "line": new_nodes.get(t, {}).get('line')}
                                                for t in ids],
                'source_excerpt': read_excerpt(repo_dir, info['file'], info['line']),
                'manual_label': 'UNREVIEWED', 'manual_evidence': ''}

    added_rows = [row(k, 'newly_resolved', new) for k in added]
    lost_rows = [row(k, 'lost_resolution', old) for k in removed]
    change_rows = [row(k, 'renaming_only', new) for k in renames_only]
    material_rows = [row(k, 'substantive_target_change', new) for k in substantive]
    for r in material_rows:
        r['old_targets_normalized'] = sorted(normalized(t, renames) for t in old[r['call_id']]['targets'])

    # Stable, balanced sample across production/test/example, old provenance,
    # edge kind and resolution cause. Avoid oversampling the same identifier.
    groups = defaultdict(deque)
    for item in added_rows:
        shape = 'attribute' if '.' in (item['source'] or '').partition('(')[0] else 'other'
        key = (item['group'], item['old_status'], item['old_origin'],
               'INSTANTIATES' in item['relations'], shape)
        groups[key].append(item)
    for key, entries in groups.items():
        groups[key] = deque(sorted(entries, key=lambda d:
            hashlib.sha256(d['call_id'].encode()).hexdigest()))
    selected = []
    groupkeys = sorted(groups, key=lambda x: (['production', 'test', 'example'].index(x[0]), x[1:]))
    while len(selected) < sample_size and any(groups.values()):
        for key in groupkeys:
            if groups[key] and len(selected) < sample_size:
                selected.append(groups[key].popleft())
    # Enrich only the bounded review sample with callee definitions and import hints.
    for record in selected:
        record['target_definition_excerpts'] = [
            {'target': d['target'], 'excerpt': read_excerpt(repo_dir, d['file'], d['line'], radius=6)}
            for d in record['target_definition_locations'][:8]]
        record['source_relevant_imports'] = relevant_imports(repo_dir, record['file'], record['source'])
    counters = Counter((r['old_status'], r['old_origin']) for r in added_rows)
    by_group = Counter(r['group'] for r in added_rows)
    return {'stats': {
        'repository': before.get('repository'), 'commit': before.get('commit'),
        'old_resolved': len(old), 'new_resolved': len(new),
        'newly_resolved': len(added_rows), 'lost_resolutions': len(lost_rows),
        'changed_targets': len(changed), 'renaming_only': len(change_rows),
        'substantive_changes': len(material_rows),
        'newly_resolved_by_previous_status': {f'{k[0]}/{k[1]}': v for k, v in sorted(counters.items())},
        'newly_resolved_by_code_area': dict(sorted(by_group.items())),
        'unverified_target_nodes': sum(not r['all_target_nodes_exist'] for r in added_rows),
        'sample_size': len(selected), 'source_excerpts_available': sum(bool(r['source_excerpt']) for r in selected)},
        'newly_resolved': added_rows, 'lost': lost_rows,
        'renaming_only': change_rows, 'substantive': material_rows,
        'sample': selected}


def write_csv(path, records):
    # Fixed schema for empty output too.
    keys = ('change', 'call_id', 'file', 'line', 'source', 'caller', 'group',
            'old_status', 'old_origin', 'new_status', 'old_targets', 'new_targets',
            'relations', 'rules', 'all_target_nodes_exist', 'target_definition_locations',
            'source_excerpt', 'source_relevant_imports', 'target_definition_excerpts',
            'manual_label', 'manual_evidence', 'old_targets_normalized')
    with path.open('w', encoding='utf-8-sig', newline='') as f:
        writer = csv.DictWriter(f, fieldnames=keys, extrasaction='ignore')
        writer.writeheader()
        for row in records:
            writer.writerow({k: json.dumps(row[k], ensure_ascii=False) if isinstance(row.get(k), (list, dict))
                             else row.get(k, '') for k in keys})


def write_outputs(output, data):
    output.mkdir(parents=True, exist_ok=True)
    for name, records in (('ALL_NEWLY_RESOLVED', data['newly_resolved']),
                          ('MANUAL_REVIEW_SAMPLE', data['sample']),
                          ('SUBSTANTIVE_TARGET_CHANGES', data['substantive']),
                          ('LOST_RESOLUTIONS', data['lost'])):
        write_csv(output / (name + '.csv'), records)
    (output / 'STATS.json').write_text(json.dumps(data['stats'], ensure_ascii=False, indent=2), encoding='utf-8')
    s = data['stats']
    lines = [f"# {s['repository']} — Source Root 변경 감사", '',
             f"- 분석 커밋: `{s['commit']}`", f"- 이전/신규 해석 호출 지점: {s['old_resolved']} / {s['new_resolved']}",
             f"- 신규 해석: **{s['newly_resolved']}**; 기존 해석 소실: **{s['lost_resolutions']}**",
             f"- 대상 변경: {s['changed_targets']} (이름 정규화만 {s['renaming_only']}, 실질적 변화 {s['substantive_changes']})",
             f"- 새 타깃 노드 없는 호출: {s['unverified_target_nodes']}",
             f"- 수작업 확인 대상: {s['sample_size']} (소스 발췌 포함 {s['source_excerpts_available']})", '',
             '## 기존 상태별 새 해결 분포', '', '| 이전 상태/출처 | 개수 |', '|---|---:|']
    lines.extend(f'| {key or "(상태 정보 없음)"} | {v} |' for key, v in s['newly_resolved_by_previous_status'].items())
    lines.extend(['', '## 파일 영역별 분포', '', '| 영역 | 개수 |', '|---|---:|'])
    lines.extend(f'| {key} | {v} |' for key, v in s['newly_resolved_by_code_area'].items())
    lines.extend(['', '## 해석 주의', '',
                  '- 새로 연결한 대상의 정의가 존재하는지와 실제 실행 시 그 대상이 호출되는지는 다른 문제다.',
                  '- **신규 연결 수는 TP 확정치가 아니다.** `MANUAL_REVIEW_SAMPLE.csv`의 소스와 정의 위치를 독립적으로 확인해야 한다.',
                  '- UNKNOWN에서 RESOLVED로 바뀐 것과 KNOWN_NON_INTERNAL에서 바뀐 것은 별도 감사 대상으로 유지한다.',
                  '- 변경 간선 중 CONSTRUCTOR 추가/다형성 MAY_CALL은 선언 타입과 실제 수신자 구별이 필요하다.',
                  '- 표본은 오류 원인 다양성을 위한 층화 표본이며 전체 Precision 추정용 확률 표본이 아니다.'])
    (output / 'SUMMARY_KO.md').write_text('\n'.join(lines) + '\n', encoding='utf-8')


def main():
    p = argparse.ArgumentParser(description='Audit old/new KnitCode graphs without executing target project')
    p.add_argument('before', type=Path)
    p.add_argument('after', type=Path)
    p.add_argument('--repo-root', type=Path, help='Source directory for review-only excerpts')
    p.add_argument('--rename', action='append', default=[], help='Canonical module renaming, e.g. src.click=click')
    p.add_argument('--sample-size', type=int, default=50)
    p.add_argument('--output-dir', type=Path, default=Path('source_root_audit'))
    args = p.parse_args()
    renames = []
    for item in args.rename:
        if '=' not in item:
            p.error('--rename requires old=new')
        a, b = item.split('=', 1)
        if not a or not b:
            p.error('--rename requires nonempty old=new')
        renames.append((a, b))
    if args.sample_size < 0:
        p.error('--sample-size must be nonnegative')
    data = analyze(load(args.before), load(args.after),
                   renames=renames, repo_dir=args.repo_root, sample_size=args.sample_size)
    write_outputs(args.output_dir, data)
    for key, value in data['stats'].items():
        if isinstance(value, int):
            print(f'{key}: {value}')
    print('Report:', args.output_dir / 'SUMMARY_KO.md')


if __name__ == '__main__':
    main()
