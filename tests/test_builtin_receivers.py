"""Type-origin regression and negative controls for built-in methods."""
import tempfile
import unittest
from pathlib import Path
from ast_parser import scan_repository
from symbol_builder import build_symbol_result
from graph_builder import build_graph
from call_status import annotate_call_statuses
from unresolved_classifier import reclassify_graph
from verify_v231 import check_status


class BuiltinReceiverTests(unittest.TestCase):
    def run_source(self, source):
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / 'app.py').write_text(source, encoding='utf-8')
            sy = build_symbol_result(scan_repository(tmp))
            g = annotate_call_statuses(reclassify_graph(build_graph(sy), sy), sy)
            self.assertEqual(check_status(g, sy), 0)
            return {x['line']: x for x in g['call_statuses']}, g

    def test_string_variable_and_return_type(self):
        rows, _ = self.run_source('def go(xs):\n    s = ","\n    s.join(xs)\n    s = ",".join(xs)\n    s.strip()\n')
        self.assertEqual(rows[3]['status'], 'KNOWN_NON_INTERNAL')
        self.assertEqual(rows[5]['status'], 'KNOWN_NON_INTERNAL')

    def test_list_and_dict_with_reassignment(self):
        rows, _ = self.run_source('def go(other):\n    x = []\n    x.append(1)\n    d = {"a": 1}\n    d.get("a")\n    x = other\n    x.append(2)\n')
        self.assertEqual(rows[3]['status'], 'KNOWN_NON_INTERNAL')
        self.assertEqual(rows[5]['status'], 'KNOWN_NON_INTERNAL')
        self.assertEqual(rows[7]['status'], 'UNKNOWN')

    def test_conditional_mixed_type_not_proven(self):
        rows, _ = self.run_source('def go(flag, other):\n    if flag:\n        s = ","\n    else:\n        s = other\n    s.join([])\n')
        self.assertEqual(rows[6]['status'], 'UNKNOWN')

    def test_unknown_object_method_never_guessed(self):
        rows, _ = self.run_source('def go(obj):\n    obj.join([])\n    obj.get("a")\n    obj.append(1)\n')
        self.assertTrue(all(x['status'] == 'UNKNOWN' for x in rows.values()))

    def test_custom_class_method_has_internal_priority(self):
        rows, g = self.run_source('class A:\n    def join(self, xs): pass\ndef go():\n    x = A()\n    x.join([])\n')
        self.assertEqual(rows[5]['status'], 'RESOLVED_INTERNAL')
        self.assertEqual(g['resolved_call_sites'], 2)

    def test_shadowed_builtin_constructor_is_not_builtin_type(self):
        rows, _ = self.run_source('class list:\n    pass\ndef go():\n    x = list()\n    x.append(1)\n')
        self.assertEqual(rows[5]['status'], 'UNKNOWN')

    def test_constructor_based_and_direct_literal(self):
        rows, _ = self.run_source('def go():\n    d = dict()\n    d.keys()\n    [].append(1)\n    b"_".join([])\n')
        for row in rows.values():
            if row['callee_text'] in ('d.keys', '[].append', 'b"_".join'):
                self.assertEqual(row['status'], 'KNOWN_NON_INTERNAL')

    def test_specific_instance_override_not_certified(self):
        rows, _ = self.run_source('def go(other):\n    x = []\n    x.append = other\n    x.append(1)\n')
        self.assertEqual(rows[4]['status'], 'UNKNOWN')


if __name__ == '__main__': unittest.main()
