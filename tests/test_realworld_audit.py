"""Audit workflow tests using synthetic pinned graph outputs."""
from pathlib import Path
import json
import subprocess
import sys
import tempfile
import unittest

from audit_realworld_changes import analyze, write_outputs, normalized


def graph(symbols, edges, statuses=None, repo='Click', commit='abc'):
    return {'repository': repo, 'commit': commit, 'nodes': symbols, 'edges': edges,
            'call_statuses': statuses or []}


def e(cid, target, source='click.echo("Hi")', file='Click/tests/test_example.py',
      line=4, rule='imported_module'):
    return {'source': 'METHOD:tests.test_example.go', 'target': target,
            'type': 'INSTANTIATES' if target.startswith('CLASS:') else 'CALLS',
            'evidence': {'call_id': cid, 'file': file, 'line': line,
                         'source': source, 'rule': rule}}


class AuditTests(unittest.TestCase):
    def make_graphs(self):
        nodes = [
            {'id': 'FUNCTION:click.utils.echo', 'file': 'Click/src/click/utils.py', 'line': 50},
            {'id': 'METHOD:click.core.Group.__init__', 'file': 'Click/src/click/core.py', 'line': 100},
            {'id': 'CLASS:tests.test_example.CustomGroup', 'file': 'Click/tests/test_example.py', 'line': 3}]
        ids = ['CALL:Click/tests/test_example.py:4:1:1',
               'CALL:Click/tests/test_example.py:5:1:2',
               'CALL:Click/tests/test_example.py:6:1:3']
        old = graph(nodes, [e(ids[1], 'FUNCTION:src.click.utils.echo', line=5),
                            e(ids[2], 'CLASS:tests.test_example.CustomGroup', line=6)])
        new = graph(nodes, [e(ids[0], 'FUNCTION:click.utils.echo'),
                            e(ids[1], 'FUNCTION:click.utils.echo', line=5),
                            e(ids[2], 'CLASS:tests.test_example.CustomGroup', line=6),
                            e(ids[2], 'METHOD:click.core.Group.__init__', line=6)])
        old['call_statuses'] = [
            {'call_id': ids[0], 'file': 'Click/tests/test_example.py', 'line': 4,
             'callee_text': 'click.echo', 'status': 'KNOWN_NON_INTERNAL', 'origin_kind': 'EXTERNAL_IMPORT'},
            {'call_id': ids[1], 'file': 'Click/tests/test_example.py', 'line': 5,
             'callee_text': 'click.echo', 'status': 'RESOLVED_INTERNAL'},
            {'call_id': ids[2], 'file': 'Click/tests/test_example.py', 'line': 6,
             'callee_text': 'CustomGroup', 'status': 'RESOLVED_INTERNAL'}]
        new['call_statuses'] = [dict(r, status='RESOLVED_INTERNAL') for r in old['call_statuses']]
        return old, new

    def test_counts_and_rename_with_constructor(self):
        old, new = self.make_graphs()
        result = analyze(old, new, renames=[('src.click', 'click')], sample_size=10)
        s = result['stats']
        self.assertEqual((s['newly_resolved'], s['changed_targets'], s['renaming_only'],
                          s['substantive_changes'], s['lost_resolutions']), (1, 2, 1, 1, 0))
        self.assertEqual(s['newly_resolved_by_previous_status']['KNOWN_NON_INTERNAL/EXTERNAL_IMPORT'], 1)
        self.assertEqual(s['sample_size'], 1)
        self.assertEqual(s['unverified_target_nodes'], 0)
        self.assertIn('METHOD:click.core.Group.__init__', result['substantive'][0]['new_targets'])

    def test_repo_mismatch_detected(self):
        old, new = self.make_graphs()
        new['commit'] = 'def'
        with self.assertRaisesRegex(ValueError, 'Commit mismatch'):
            analyze(old, new)

    def test_call_site_source_mismatch_detected(self):
        old, new = self.make_graphs()
        new['call_statuses'][0]['line'] = 999
        with self.assertRaisesRegex(ValueError, 'source facts'):
            analyze(old, new)

    def test_stored_path_escape_not_read(self):
        old, new = self.make_graphs()
        with tempfile.TemporaryDirectory() as temp:
            self.assertEqual(analyze(old, new, repo_dir=Path(temp))['sample'][0]['source_excerpt'], '')

    def test_writes_utf8_bom_csv_and_report(self):
        old, new = self.make_graphs()
        with tempfile.TemporaryDirectory() as temp:
            out = Path(temp)
            write_outputs(out, analyze(old, new, renames=[('src.click', 'click')]))
            self.assertTrue((out / 'SUMMARY_KO.md').is_file())
            self.assertTrue((out / 'MANUAL_REVIEW_SAMPLE.csv').read_bytes().startswith(b'\xef\xbb\xbf'))

    def test_compare_results_summary_cp949_safe(self):
        old, new = self.make_graphs()
        with tempfile.TemporaryDirectory() as temp:
            p = Path(temp)
            o, n, dest = (p / file for file in ('old.json', 'new.json', 'full.json'))
            o.write_text(json.dumps(old, ensure_ascii=False), encoding='utf-8')
            n.write_text(json.dumps(new, ensure_ascii=False), encoding='utf-8')
            script = Path(__file__).resolve().parents[1] / 'compare_results.py'
            result = subprocess.run([sys.executable, str(script), str(o), str(n), '--summary',
                                     '--output', str(dest)], capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(json.loads(dest.read_text(encoding='utf-8'))['new_resolved'], 3)
            self.assertNotIn('test_example.py', result.stdout)

    def test_target_prefix_boundary(self):
        self.assertEqual(normalized('METHOD:src.click.core.Command', [('src.click', 'click')]),
                         'METHOD:click.core.Command')
        self.assertEqual(normalized('METHOD:src.clicker.Command', [('src.click', 'click')]),
                         'METHOD:src.clicker.Command')


if __name__ == '__main__':
    unittest.main()
