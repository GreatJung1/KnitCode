"""Reclassification must be conservative; labels must never create call edges."""
import csv
import tempfile
import unittest
from pathlib import Path

from ast_parser import scan_repository
from symbol_builder import build_symbol_result
from graph_builder import build_graph
from unresolved_classifier import reclassify_graph, write_classification
from graph_report import write_html


class ClassificationTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.root = Path(self.dir.name) / 'sample'
        self.root.mkdir()

    def analyze(self, source, submodule=None):
        file = self.root / 'app.py'
        file.write_text(source, encoding='utf-8')
        if submodule:
            sub = self.root / 'pkg'
            sub.mkdir(exist_ok=True)
            (sub / '__init__.py').write_text('', encoding='utf-8')
            (sub / 'other.py').write_text(submodule, encoding='utf-8')
        ast_data = scan_repository(self.root)
        symbols = build_symbol_result(ast_data)
        graph_before = build_graph(symbols)
        original = {(e['source'], e['target'], e['type'], e['evidence'].get('call_id'))
                    for e in graph_before['edges']}
        graph = reclassify_graph(graph_before, symbols)
        after = {(e['source'], e['target'], e['type'], e['evidence'].get('call_id'))
                 for e in graph['edges']}
        self.assertEqual(original, after, 'Classification must never add or remove a call edge')
        self.assertEqual(sum(graph['classification_summary']['categories'].values()),
                         graph['unresolved_call_sites'])
        self.assertTrue(all('category' in x and 'reason' in x for x in graph['unresolved_calls']))
        return graph

    def record(self, graph, name):
        matches = [x for x in graph['unresolved_calls'] if x['callee_text'] == name]
        self.assertTrue(matches, f'not unresolved: {name}')
        return matches[-1]

    def test_builtin(self):
        g = self.analyze('def f():\n    print("hello")\n    x = set()\n')
        self.assertEqual(self.record(g, 'print')['category'], 'BUILTIN')
        self.assertEqual(self.record(g, 'set')['category'], 'BUILTIN')

    def test_shadowed_builtin_not_builtin(self):
        g = self.analyze('def f(print):\n    print("hello")\n')
        self.assertNotEqual(self.record(g, 'print')['category'], 'BUILTIN')

    def test_stdlib_module_and_alias(self):
        g = self.analyze('import os as tool\ndef f():\n    tool.path.dirname("x")\n')
        call = self.record(g, 'tool.path.dirname')
        self.assertEqual(call['category'], 'STDLIB')
        self.assertEqual(call['classification_evidence']['imported_module'], 'os')

    def test_external_from_import_at_module_level(self):
        g = self.analyze('from setuptools import setup\nsetup()\n')
        call = self.record(g, 'setup')
        self.assertEqual(call['category'], 'EXTERNAL_IMPORT')
        self.assertEqual(call['scope_context'], 'MODULE_LEVEL')
        self.assertEqual(call['reason'], 'no_enclosing_function')

    def test_unknown_unqualified_not_fake_internal(self):
        g = self.analyze('def f():\n    run()\n', 'def run(): pass')
        call = self.record(g, 'run')
        self.assertNotEqual(call['category'], 'INTERNAL_CANDIDATE')
        self.assertEqual(call['candidate_targets'], [])

    def test_module_call_internal_candidate(self):
        g = self.analyze('def launch(): pass\nlaunch()\n')
        # v2.2: real internal module-level calls are graph edges, not just triage.
        edges = [e for e in g['edges'] if e['source'] == 'MODULE:app'
                 and e['target'] == 'FUNCTION:app.launch']
        self.assertEqual(len(edges), 1)
        self.assertTrue(edges[0]['evidence']['module_level'])

    def test_indirect_call_via_loop(self):
        g = self.analyze('def f(items):\n    for enum in items:\n        enum()\n')
        call = self.record(g, 'enum')
        self.assertEqual(call['category'], 'INDIRECT_CALL')
        self.assertEqual(call['classification_evidence']['assignment_type'], 'FOR_TARGET')

    def test_collection_method_heuristic(self):
        g = self.analyze('def f():\n    data=[]\n    data.append(1)\n')
        call = self.record(g, 'data.append')
        self.assertEqual(call['category'], 'BUILTIN_METHOD')
        self.assertEqual(call['confidence'], 'medium')

    def test_chained_field(self):
        g = self.analyze('class A:\n    def f(self):\n        self.queue.put(1)\n')
        call = self.record(g, 'self.queue.put')
        self.assertEqual(call['category'], 'FIELD_CHAIN')

    def test_unknown_receiver(self):
        g = self.analyze('def f(input_obj):\n    input_obj.work()\n')
        call = self.record(g, 'input_obj.work')
        self.assertEqual(call['category'], 'RECEIVER_UNKNOWN')

    def test_export_csv_and_html(self):
        g = self.analyze('def f():\n    print("Hello")\n')
        out = Path(self.dir.name) / 'out'
        write_classification(g, out)
        write_html(g, out / 'report.html')
        with (out / 'unresolved_classification.csv').open(encoding='utf-8-sig', newline='') as f:
            rows = list(csv.DictReader(f))
        self.assertEqual(len(rows), g['unresolved_call_sites'])
        self.assertIn('BUILTIN', [r['category'] for r in rows])
        self.assertIn('재분류', (out / 'report.html').read_text(encoding='utf-8'))
        self.assertTrue((out / 'unresolved_classification.json').exists())

if __name__ == '__main__':
    unittest.main()
