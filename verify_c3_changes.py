"""Reproduce old/new graphs and exporter compatibility without running fixtures."""
import difflib
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys


def main():
    work = Path(__file__).resolve().parent
    original = work.parent / 'KnitCode_v24_extended'
    out = work / 'validation'
    fixture = out / 'fixture'
    fixture.mkdir(parents=True, exist_ok=True)
    (fixture / 'app.py').write_text('''class A:
    def run(self): pass
class B(A): pass
class C(A):
    def run(self): pass
class D(B, C):
    def go(self): super().run()
class E(Unknown, A): pass
def example():
    obj = D()
    obj.run()
    other = E()
    other.run()
''', encoding='utf-8')
    files = [p for p in original.rglob('*') if p.is_file() and '__pycache__' not in p.parts]
    digest = lambda p: hashlib.sha256(p.read_bytes()).hexdigest()
    before = {p.relative_to(original).as_posix(): digest(p) for p in files}
    graphs = {}
    for label, engine in [('before', original), ('after', work)]:
        result = subprocess.run([sys.executable, str(engine / 'main.py'), str(fixture),
                                 '--output-dir', str(out / label)], cwd=engine,
                                env={**os.environ, 'PYTHONDONTWRITEBYTECODE': '1'},
                                capture_output=True, text=True, encoding='utf-8')
        (out / (label + '_cli.log')).write_text(result.stdout + result.stderr, encoding='utf-8')
        if result.returncode:
            raise RuntimeError(result.stderr)
        graphs[label] = json.loads((out / label / 'graph_result.json').read_text(encoding='utf-8'))
        for filename in ('report.html', 'call_statuses.csv', 'graph_result.json'):
            assert (out / label / filename).stat().st_size > 0, filename
    assert graphs['before'].keys() == graphs['after'].keys(), 'Graph schema changed'
    differences = []
    for old, new in zip(graphs['before']['call_statuses'], graphs['after']['call_statuses']):
        assert old.keys() == new.keys(), 'Call status schema changed'
        if old['target_ids'] != new['target_ids']:
            differences.append({'callee': old['callee_text'], 'line': old['line'],
                                'before': old['target_ids'], 'after': new['target_ids'],
                                'before_status': old['status'], 'after_status': new['status']})
    after = {p.relative_to(original).as_posix(): digest(p) for p in files}
    assert before == after, 'Original was modified'
    (out / 'original_sha256.json').write_text(json.dumps(after, indent=2), encoding='utf-8')
    changed, patch = [], []
    for p in files:
        rel = p.relative_to(original)
        candidate = work / rel
        if candidate.exists() and digest(candidate) != digest(p):
            changed.append(rel.as_posix())
            patch.extend(difflib.unified_diff(p.read_text(encoding='utf-8').splitlines(True),
                                            candidate.read_text(encoding='utf-8').splitlines(True),
                                            fromfile='v24/' + rel.as_posix(), tofile='v25/' + rel.as_posix()))
    (out / 'implementation.patch').write_text(''.join(patch), encoding='utf-8')
    summary = {'original_unchanged_during_verification': before == after,
               'changed_existing_files': changed, 'graph_schema_preserved': True,
               'export_smoke_test': 'JSON/CSV/HTML passed', 'call_graph_changes': differences}
    (out / 'comparison.json').write_text(json.dumps(summary, indent=2, ensure_ascii=False), encoding='utf-8')
    print(json.dumps(summary, indent=2, ensure_ascii=False))


if __name__ == '__main__':
    main()
