"""Check compatibility/preservation and export one real analysis result."""
import difflib
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import os


def main():
    work = Path(__file__).resolve().parent
    base = work.parent / 'KnitCode_v25_c3'
    out = work / 'validation' / 'object_flow'
    digest = lambda p: hashlib.sha256(p.read_bytes()).hexdigest()
    source = [p for p in base.rglob('*') if p.is_file() and '__pycache__' not in p.parts]
    before = {p.relative_to(base).as_posix(): digest(p) for p in source}
    for old in (base / 'tests').rglob('*.py'):
        assert digest(old) == digest(work / old.relative_to(base)), old
    fixture = work.parent / 'knitcode_v25_benchmark' / 'cases' / '14_field_factory'
    result = subprocess.run([sys.executable, str(work / 'main.py'), str(fixture), '--output-dir', str(out / 'export')],
                            cwd=work, env={**os.environ, 'PYTHONDONTWRITEBYTECODE': '1'},
                            capture_output=True, text=True, encoding='utf-8')
    (out / 'export.log').write_text(result.stdout + result.stderr, encoding='utf-8')
    assert result.returncode == 0, result.stderr
    for name in ('graph_result.json', 'report.html', 'call_statuses.csv'):
        assert (out / 'export' / name).stat().st_size
    graph = json.loads((out / 'export' / 'graph_result.json').read_text(encoding='utf-8'))
    new_edges = [e for e in graph['edges'] if e['evidence'].get('rule') == 'object_flow_may_call']
    assert new_edges
    assert all(e['evidence'].get(k) for e in new_edges for k in ('call_id', 'file', 'line', 'source', 'trace', 'certainty'))
    old_measure = json.loads((out / 'measurement_before.json').read_text(encoding='utf-8'))
    new_measure = json.loads((out / 'measurement_after.json').read_text(encoding='utf-8'))
    assert old_measure['input_sha256'] == new_measure['input_sha256']
    assert old_measure['schemas'] == new_measure['schemas']
    after = {p.relative_to(base).as_posix(): digest(p) for p in source}
    assert before == after
    patch, changed = [], []
    for old in base.glob('*.py'):
        new = work / old.name
        if digest(old) != digest(new):
            changed.append(old.name)
            patch.extend(difflib.unified_diff(old.read_text(encoding='utf-8').splitlines(True),
                                            new.read_text(encoding='utf-8').splitlines(True),
                                            fromfile='c3/' + old.name, tofile='object_flow/' + old.name))
    for new in [work / 'object_flow.py', *sorted((work / 'tests').glob('test_*flow.py')), work / 'tests' / 'test_context_dispatch.py']:
        if not (base / new.relative_to(work)).exists():
            patch.extend(difflib.unified_diff([], new.read_text(encoding='utf-8').splitlines(True),
                                            fromfile='/dev/null', tofile='object_flow/' + new.relative_to(work).as_posix()))
    (out / 'implementation.patch').write_text(''.join(patch), encoding='utf-8')
    (out / 'c3_sha256.json').write_text(json.dumps(after, indent=2), encoding='utf-8')
    summary = {'original_c3_unchanged_during_verification': before == after,
               'all_existing_test_files_unchanged': True, 'changed_existing_engine_files': changed,
               'all_22_graph_and_status_key_sets_preserved': True, 'JSON_CSV_HTML_export': 'PASS',
               'new_edge_evidence': 'PASS',
               'time_ratio': new_measure['median_seconds'] / old_measure['median_seconds'],
               'traced_memory_ratio': new_measure['median_peak_bytes'] / old_measure['median_peak_bytes']}
    (out / 'verification.json').write_text(json.dumps(summary, indent=2), encoding='utf-8')
    print(json.dumps(summary, indent=2))


if __name__ == '__main__':
    main()
