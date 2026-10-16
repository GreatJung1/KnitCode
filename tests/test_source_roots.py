"""Source-root import identity regression tests (no execution of target code)."""
import tempfile
import unittest
from pathlib import Path

from ast_parser import scan_repository
from symbol_builder import build_symbol_result
from graph_builder import build_graph
from unresolved_classifier import reclassify_graph
from call_status import annotate_call_statuses


class SourceRootTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.root = Path(self.tempdir.name) / 'repository'
        self.root.mkdir()

    def write(self, path, text):
        out = self.root / path
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(text, encoding='utf-8')

    def analyze(self, source_roots=None):
        facts = scan_repository(self.root, source_roots=source_roots)
        self.assertEqual(facts['parse_errors'], [])
        symbols = build_symbol_result(facts)
        graph = annotate_call_statuses(reclassify_graph(build_graph(symbols), symbols), symbols)
        return facts, symbols, graph

    @staticmethod
    def targets(graph, text):
        return {edge['target'] for edge in graph['edges'] if edge['type'] == 'CALLS'
                and edge.get('evidence', {}).get('source') == text}

    def test_auto_src_layout_absolute_import(self):
        self.write('src/click/__init__.py', '')
        self.write('src/click/shell_completion.py',
                   'class CompletionItem:\n    def __init__(self, name): self.name = name\n')
        self.write('src/click/core.py',
                   'from click.shell_completion import CompletionItem\n'
                   'def get():\n    return CompletionItem("word")\n')
        facts, symbols, graph = self.analyze()
        self.assertEqual(facts['source_roots'], ['src'])
        self.assertIn('CLASS:click.shell_completion.CompletionItem',
                      {s['id'] for s in symbols['symbols']})
        self.assertIn('CLASS:click.shell_completion.CompletionItem',
                      {e['target'] for e in graph['edges'] if e['type'] == 'INSTANTIATES'})
        self.assertEqual(graph['resolved_call_sites'], 1)

    def test_relative_import_and_reexport_with_src_layout(self):
        self.write('src/demo/__init__.py', 'from .api import make\n')
        self.write('src/demo/api.py', 'def make(): pass\n')
        self.write('src/demo/consumer.py', 'from demo import make\ndef use(): make()\n')
        _, _, graph = self.analyze()
        self.assertEqual(self.targets(graph, 'make()'), {'FUNCTION:demo.api.make'})

    def test_external_same_named_callable_is_not_internal(self):
        self.write('src/demo/__init__.py', '')
        self.write('src/demo/api.py', 'def parse(): pass\n')
        self.write('src/demo/consumer.py',
                   'from thirdparty.api import parse\ndef use(): parse()\n')
        _, _, graph = self.analyze()
        self.assertEqual(self.targets(graph, 'parse()'), set())
        self.assertEqual(graph['resolved_call_sites'], 0)

    def test_src_itself_a_package_is_not_import_root(self):
        self.write('src/__init__.py', '')
        self.write('src/demo/__init__.py', '')
        self.write('src/demo/api.py', 'def run(): pass\n')
        facts, symbols, _ = self.analyze()
        self.assertEqual(facts['source_roots'], [])
        self.assertIn('FUNCTION:src.demo.api.run', {s['id'] for s in symbols['symbols']})

    def test_explicit_root_opt_out(self):
        self.write('src/demo/__init__.py', '')
        self.write('src/demo/api.py', 'def run(): pass\n')
        facts, symbols, _ = self.analyze(source_roots=['.'])
        self.assertEqual(facts['source_roots'], [])
        self.assertIn('FUNCTION:src.demo.api.run', {s['id'] for s in symbols['symbols']})

    def test_explicit_nested_root(self):
        self.write('lib/python/demo/__init__.py', '')
        self.write('lib/python/demo/api.py', 'def run(): pass\n')
        facts, symbols, _ = self.analyze(source_roots=['lib/python'])
        self.assertEqual(facts['source_roots'], ['lib/python'])
        self.assertIn('FUNCTION:demo.api.run', {s['id'] for s in symbols['symbols']})

    def test_ambiguous_package_collision_keeps_legacy_identity(self):
        self.write('demo/__init__.py', '')
        self.write('src/demo/__init__.py', '')
        facts, symbols, _ = self.analyze()
        self.assertEqual(facts['source_roots'], [])
        self.assertIn('src.demo', symbols['source_modules'])

    def test_invalid_explicit_root_rejected(self):
        with self.assertRaises(ValueError):
            scan_repository(self.root, source_roots=['../outside'])


if __name__ == '__main__':
    unittest.main()
