"""Source backed dependency links, reexports, inheritance and safety controls."""
import tempfile
import unittest
from pathlib import Path
from ast_parser import scan_repository
from symbol_builder import build_symbol_result
from graph_builder import build_graph
from call_status import annotate_call_statuses
from unresolved_classifier import reclassify_graph
from dependency_analysis import analyze_dependencies


class DependencyAnalysisTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.app = self.root / 'app'; self.app.mkdir()
        self.vendor = self.root / 'vendor'; self.vendor.mkdir()
        self.pkg = self.vendor / 'mypkg'; self.pkg.mkdir()
        (self.pkg / '__init__.py').write_text(
            'raise RuntimeError("MUST NOT EXECUTE")\nfrom .api import run, Client\n', encoding='utf8')
        (self.pkg / 'api.py').write_text(
            'from .utils import helper\nclass Client:\n    def save(self): return helper()\n'
            'def run(): return helper()\n', encoding='utf8')
        (self.pkg / 'utils.py').write_text('def helper(): return 1\n', encoding='utf8')

    def analyze(self, source, **opts):
        (self.app / 'app.py').write_text(source, encoding='utf8')
        sy = build_symbol_result(scan_repository(self.app))
        g = annotate_call_statuses(reclassify_graph(build_graph(sy), sy), sy)
        dep = analyze_dependencies(sy, g, roots=[self.vendor], **opts)
        return dep, g

    @staticmethod
    def call(result, callee):
        matches = [x for x in result['project_calls'] if x['callee_text'] == callee]
        assert len(matches) == 1, matches
        return matches[0]

    def test_external_reexport_function_and_class_method(self):
        dep, _ = self.analyze('from mypkg import run, Client\ndef test():\n    run()\n    obj = Client()\n    obj.save()\n')
        self.assertEqual(self.call(dep, 'run')['target_ids'], ['DEP:FUNCTION:mypkg.api.run'])
        self.assertEqual(self.call(dep, 'Client')['target_ids'], ['DEP:CLASS:mypkg.api.Client'])
        self.assertEqual(self.call(dep, 'obj.save')['target_ids'], ['DEP:METHOD:mypkg.api.Client.save'])
        # The indexed dependency source is inspected without importing it.
        self.assertTrue(any(e['target'] == 'DEP:FUNCTION:mypkg.utils.helper'
                            for e in dep['dependency_edges']))

    def test_stdlib_source_definition(self):
        dep, _ = self.analyze('import json\ndef test():\n    json.dumps({})\n')
        self.assertEqual(self.call(dep, 'json.dumps')['result'], 'SOURCE_FOUND')
        self.assertTrue(any('json.dumps' in x for x in self.call(dep, 'json.dumps')['target_ids']))

    def test_platform_os_path_join_not_assumed_unique(self):
        dep, _ = self.analyze('import os\ndef test():\n    os.path.join("a", "b")\n')
        row = self.call(dep, 'os.path.join')
        self.assertEqual(row['result'], 'SOURCE_FOUND')
        self.assertTrue(all('posixpath.join' in x or 'ntpath.join' in x for x in row['target_ids']))
        self.assertTrue(all('PLATFORM_DEPENDENT' in str(e) for e in dep['project_edges']))

    def test_shadowed_import_avoids_false_source_link(self):
        dep, _ = self.analyze('from mypkg import run\ndef test(obj):\n    run = obj\n    run()\n')
        self.assertFalse(dep['project_edges'])

    def test_monkeypatched_module_attribute_is_not_proved(self):
        dep, _ = self.analyze('import json\ndef test(fn):\n    json.dumps = fn\n    json.dumps({})\n')
        self.assertFalse(any('json.dumps' in e['target'] for e in dep['project_edges']))

    def test_unknown_external_api_is_origin_only(self):
        dep, _ = self.analyze('import mypkg\ndef test():\n    mypkg.something_nonexistent()\n')
        self.assertEqual(self.call(dep, 'mypkg.something_nonexistent')['result'], 'ORIGIN_ONLY_NO_DEFINITION')

    def test_stub_is_api_not_executable_source(self):
        (self.vendor / 'stublib.pyi').write_text('def calculate(x: int) -> int: ...\n', encoding='utf8')
        dep, _ = self.analyze('import stublib\ndef test():\n    stublib.calculate(4)\n')
        self.assertEqual(self.call(dep, 'stublib.calculate')['result'], 'API_STUB_ONLY')

    def test_dependency_budget_is_explicit(self):
        dep, _ = self.analyze('from mypkg import run\ndef test():\n    run()\n', max_files=1)
        self.assertTrue(dep['summary']['truncated'])
        self.assertEqual(dep['summary']['indexed_dependency_python_files'], 1)

    def test_instance_overwrite_invalidates_source_method(self):
        dep, _ = self.analyze('from mypkg import Client\ndef test(callback):\n'
                              '    a = Client()\n    a.save = callback\n    a.save()\n')
        self.assertFalse(any('Client.save' in e['target'] for e in dep['project_edges']))


if __name__ == '__main__': unittest.main()
