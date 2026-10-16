"""Join-origin regression tests: proven provenance versus unknown receivers."""
import tempfile
import unittest
from pathlib import Path

from ast_parser import scan_repository
from symbol_builder import build_symbol_result
from graph_builder import build_graph
from unresolved_classifier import reclassify_graph
from call_status import annotate_call_statuses
from verify_v231 import check_status


class JoinOriginTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / 'src'
        self.root.mkdir()

    def analyze(self, source):
        (self.root / 'app.py').write_text(source, encoding='utf-8')
        facts = scan_repository(self.root)
        self.assertFalse(facts['parse_errors'])
        symbols = build_symbol_result(facts)
        graph = annotate_call_statuses(reclassify_graph(build_graph(symbols), symbols), symbols)
        self.assertEqual(check_status(graph, symbols), 0)
        return graph

    def row(self, graph, callee):
        matches = [r for r in graph['call_statuses'] if r['callee_text'] == callee]
        self.assertEqual(len(matches), 1, (callee, matches))
        return matches[0]

    def test_literal_string_bytes_fstring_join(self):
        graph = self.analyze('def run(items, x):\n'
                             '    ",".join(items)\n'
                             "    b':'.join(items)\n"
                             '    f"a{x}".join(items)\n')
        for callee in ('",".join', "b':'.join", 'f"a{x}".join'):
            row = self.row(graph, callee)
            self.assertEqual(row['status'], 'KNOWN_NON_INTERNAL', callee)
            self.assertEqual(row['origin_kind'], 'BUILTIN', callee)
            self.assertEqual(row['legacy_category'], 'BUILTIN_METHOD', callee)
            self.assertEqual(row['evidence']['rule'], 'LITERAL_BUILTIN_JOIN', callee)

    def test_outer_chained_call_is_not_mislabelled(self):
        graph = self.analyze('def run(items):\n    return ",".join(items).strip()\n')
        self.assertEqual(self.row(graph, '",".join')['status'], 'KNOWN_NON_INTERNAL')
        self.assertEqual(self.row(graph, '",".join(items).strip')['status'], 'KNOWN_NON_INTERNAL')

    def test_simple_string_variable_resolved_but_unknown_object_remains_unknown(self):
        graph = self.analyze('def run(obj, items):\n    s = ","\n    s.join(items)\n    obj.join()\n')
        self.assertEqual(self.row(graph, 's.join')['status'], 'KNOWN_NON_INTERNAL')
        self.assertEqual(self.row(graph, 'obj.join')['status'], 'UNKNOWN')

    def test_thread_constructor_origin_and_reassignment(self):
        graph = self.analyze('import threading\n'
                             'def run(other):\n'
                             '    t = threading.Thread()\n'
                             '    t.join()\n'
                             '    t = other\n'
                             '    t.join()\n')
        rows = [r for r in graph['call_statuses'] if r['callee_text'] == 't.join']
        self.assertEqual(len(rows), 2)
        self.assertEqual([r['status'] for r in rows], ['KNOWN_NON_INTERNAL', 'UNKNOWN'])
        self.assertEqual(rows[0]['origin_kind'], 'STDLIB')
        self.assertEqual(rows[0]['evidence']['rule'], 'FLOW_STDLIB_JOIN')

    def test_multiple_external_inherited_candidates(self):
        graph = self.analyze('import multiprocessing\n'
                             'class A(multiprocessing.Process): pass\n'
                             'class B(multiprocessing.Process): pass\n'
                             'def run(flag):\n'
                             '    if flag:\n'
                             '        obj = A()\n'
                             '    else:\n'
                             '        obj = B()\n'
                             '    obj.join()\n')
        row = self.row(graph, 'obj.join')
        self.assertEqual(row['status'], 'KNOWN_NON_INTERNAL')
        self.assertEqual(row['origin_kind'], 'STDLIB')

    def test_local_override_not_assumed_stdlib(self):
        graph = self.analyze('import multiprocessing\n'
                             'class A(multiprocessing.Process):\n'
                             '    def join(self): pass\n'
                             'def run():\n'
                             '    obj = A()\n'
                             '    obj.join()\n')
        row = self.row(graph, 'obj.join')
        self.assertEqual(row['status'], 'RESOLVED_INTERNAL')
        self.assertEqual(row['origin_kind'], 'INTERNAL')

    def test_unknown_preceding_mro_base_not_guessed(self):
        graph = self.analyze('import multiprocessing\n'
                             'class A(UndocumentedBase, multiprocessing.Process): pass\n'
                             'def run():\n'
                             '    obj = A()\n'
                             '    obj.join()\n')
        self.assertEqual(self.row(graph, 'obj.join')['status'], 'UNKNOWN')

    def test_conditional_import_not_proven(self):
        graph = self.analyze('if flag: import threading\n'
                             'def run():\n'
                             '    obj = threading.Thread()\n'
                             '    obj.join()\n')
        self.assertEqual(self.row(graph, 'obj.join')['status'], 'UNKNOWN')

    def test_rebound_module_constructor_is_not_certified(self):
        graph = self.analyze('import threading\n'
                             'threading.Thread = get_custom()\n'
                             'def run():\n'
                             '    t = threading.Thread()\n'
                             '    t.join()\n')
        self.assertEqual(self.row(graph, 't.join')['status'], 'UNKNOWN')

    def test_instance_join_monkeypatch_is_not_certified(self):
        graph = self.analyze('import threading\n'
                             'def run(callback):\n'
                             '    t = threading.Thread()\n'
                             '    t.join = callback\n'
                             '    t.join()\n')
        self.assertEqual(self.row(graph, 't.join')['status'], 'UNKNOWN')

    def test_unknown_project_join_method_remains_internal_if_found(self):
        graph = self.analyze('class Client:\n'
                             '    def join(self): pass\n'
                             'def run():\n'
                             '    c = Client()\n'
                             '    c.join()\n')
        self.assertEqual(self.row(graph, 'c.join')['status'], 'RESOLVED_INTERNAL')


if __name__ == '__main__':
    unittest.main()
