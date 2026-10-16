"""v2.3.1 status tests: provenance, conservative shadowing, no graph mutation."""
import csv
import json
from pathlib import Path
import tempfile
import unittest

from ast_parser import scan_repository
from symbol_builder import build_symbol_result
from graph_builder import build_graph
from unresolved_classifier import reclassify_graph
from call_status import annotate_call_statuses, write_call_statuses
from graph_report import write_html
from verify_v231 import check_status, check_backward


class StatusTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.repo = Path(self.tmp.name) / 'src'
        self.pkg = self.repo / 'sample'
        self.pkg.mkdir(parents=True)
        (self.pkg / '__init__.py').write_text('', encoding='utf-8')

    def put(self, name, src):
        (self.pkg / (name + '.py')).write_text(src, encoding='utf-8')

    def analyze(self):
        ast = scan_repository(self.repo)
        self.assertFalse(ast['parse_errors'])
        symbols = build_symbol_result(ast)
        old = reclassify_graph(build_graph(symbols), symbols)
        new = annotate_call_statuses(reclassify_graph(build_graph(symbols), symbols), symbols)
        self.assertEqual(check_backward(old, new), 0)
        self.assertEqual(check_status(new, symbols), 0)
        return new

    @staticmethod
    def get(g, text):
        result = [x for x in g['call_statuses'] if x['callee_text'] == text]
        if not result:
            raise AssertionError('no call found: ' + text)
        return result[0]

    def test_three_way_partition(self):
        self.put('app', 'import os\nclass A:\n    def run(self): pass\ndef f(obj):\n    a = A()\n    a.run()\n    os.getcwd()\n    obj.close()\n    print("ok")\n')
        g = self.analyze()
        self.assertEqual(self.get(g, 'a.run')['status'], 'RESOLVED_INTERNAL')
        self.assertEqual(self.get(g, 'os.getcwd')['status'], 'KNOWN_NON_INTERNAL')
        self.assertEqual(self.get(g, 'os.getcwd')['origin_kind'], 'STDLIB')
        self.assertEqual(self.get(g, 'obj.close')['status'], 'UNKNOWN')
        self.assertEqual(self.get(g, 'print')['origin_kind'], 'BUILTIN')

    def test_local_reassignment_overrides_import(self):
        self.put('app', 'import os\nclass A:\n    def run(self): pass\ndef go():\n    os = A()\n    os.run()\n')
        g = self.analyze()
        self.assertEqual(self.get(g, 'os.run')['status'], 'RESOLVED_INTERNAL')
        self.assertNotEqual(self.get(g, 'os.run')['origin_kind'], 'STDLIB')

    def test_module_reassignment_makes_origin_unknown(self):
        self.put('app', 'import os\nif flag:\n    os = get_other()\ndef go():\n    os.path.join("a", "b")\n')
        g = self.analyze()
        row = self.get(g, 'os.path.join')
        self.assertEqual(row['status'], 'UNKNOWN')
        self.assertEqual(row['evidence']['rule'], 'shadowing_or_conditional_binding')

    def test_builtin_is_not_assumed_when_parameter_shadows(self):
        self.put('app', 'def use(len):\n    return len([1,2])\n')
        g = self.analyze()
        self.assertEqual(self.get(g, 'len')['status'], 'UNKNOWN')

    def test_builtin_is_not_assumed_when_module_shadows(self):
        self.put('app', 'len = anything\ndef use():\n    return len([1,2])\n')
        g = self.analyze()
        self.assertEqual(self.get(g, 'len')['status'], 'UNKNOWN')

    def test_stdlib_reexport_and_builtin_alias_are_noninternal(self):
        self.put('compat', 'from urllib.parse import urlparse\nfrom builtins import str as builtin_str\n')
        self.put('app', 'from .compat import urlparse, builtin_str\ndef f(x):\n    urlparse(x)\n    builtin_str(x)\n')
        g = self.analyze()
        self.assertEqual(self.get(g, 'urlparse')['status'], 'KNOWN_NON_INTERNAL')
        self.assertEqual(self.get(g, 'urlparse')['origin_kind'], 'STDLIB')
        self.assertEqual(self.get(g, 'builtin_str')['status'], 'KNOWN_NON_INTERNAL')

    def test_conditional_import_unknown(self):
        self.put('app', 'if flag:\n    import math\ndef go():\n    math.sqrt(2)\n')
        g = self.analyze()
        self.assertEqual(self.get(g, 'math.sqrt')['status'], 'UNKNOWN')

    def test_competing_imports_unknown(self):
        self.put('app', 'try:\n    from json import loads as decoder\nexcept ImportError:\n    from simplejson import loads as decoder\ndef go(x):\n    decoder(x)\n')
        g = self.analyze()
        row = self.get(g, 'decoder')
        self.assertEqual(row['status'], 'UNKNOWN')

    def test_unconditional_external_import(self):
        self.put('app', 'import httpx as remote\ndef go():\n    remote.get("abc")\n')
        g = self.analyze()
        self.assertEqual(self.get(g, 'remote.get')['origin_kind'], 'EXTERNAL_IMPORT')
        self.assertEqual(self.get(g, 'remote.get')['status'], 'KNOWN_NON_INTERNAL')

    def test_parameter_overrides_import(self):
        self.put('app', 'import os\ndef run(os):\n    os.path.join("a","b")\n')
        g = self.analyze()
        self.assertEqual(self.get(g, 'os.path.join')['status'], 'UNKNOWN')

    def test_definition_overrides_import(self):
        self.put('app', 'from math import sqrt\ndef sqrt(x):\n    return x\ndef go():\n    sqrt(4)\n')
        g = self.analyze()
        self.assertEqual(self.get(g, 'sqrt')['status'], 'RESOLVED_INTERNAL')

    def test_legacy_symbols_without_hazards_do_not_certify_external(self):
        self.put('app', 'import os\ndef go():\n    os.getcwd()\n    print(1)\n')
        facts = scan_repository(self.repo)
        sym = build_symbol_result(facts)
        del sym['binding_hazards']  # Simulate original v2.3 symbol_result.json.
        g = annotate_call_statuses(reclassify_graph(build_graph(sym), sym), sym)
        self.assertEqual(self.get(g, 'os.getcwd')['status'], 'UNKNOWN')
        self.assertEqual(self.get(g, 'print')['status'], 'UNKNOWN')

    def test_star_import_prevents_assuming_builtin_origin(self):
        self.put('app', 'from unknown import *\ndef go():\n    print(1)\n')
        g = self.analyze()
        self.assertEqual(self.get(g, 'print')['status'], 'UNKNOWN')

    def test_global_mutation_in_sibling_function_blocks_claim(self):
        self.put('app', 'import os\ndef reset():\n    global os\n    os = replacement\ndef go():\n    os.getcwd()\n')
        g = self.analyze()
        row = self.get(g, 'os.getcwd')
        self.assertEqual(row['status'], 'UNKNOWN')
        self.assertTrue(any(x['type'] == 'GLOBAL' for x in row['evidence']['conflicts']))

    def test_walrus_assignment_blocks_import_claim(self):
        self.put('app', 'import math\ndef go(flag):\n    if flag and (math := obj): pass\n    math.sqrt(4)\n')
        g = self.analyze()
        self.assertEqual(self.get(g, 'math.sqrt')['status'], 'UNKNOWN')

    def test_except_binding_blocks_import_claim(self):
        self.put('app', 'import os\ndef go():\n    try: raise ValueError()\n    except ValueError as os: pass\n    os.getcwd()\n')
        g = self.analyze()
        self.assertEqual(self.get(g, 'os.getcwd')['status'], 'UNKNOWN')

    def test_source_backed_list_literal_method_is_certified(self):
        self.put('app', 'def go():\n    data = []\n    data.append(3)\n')
        g = self.analyze()
        row = self.get(g, 'data.append')
        self.assertEqual(row['legacy_category'], 'BUILTIN_METHOD')
        self.assertEqual(row['status'], 'KNOWN_NON_INTERNAL')
        self.assertEqual(row['evidence']['rule'], 'FLOW_BUILTIN_METHOD')

    def test_internal_import_needs_graph_edge(self):
        self.put('api', 'def f(): pass\n')
        self.put('app', 'from .api import f\ndef go():\n    f()\n')
        g = self.analyze()
        self.assertEqual(self.get(g, 'f')['status'], 'RESOLVED_INTERNAL')

    def test_report_exports_all_call_sites(self):
        self.put('app', 'import os\ndef go(x):\n    print("hi")\n    os.getcwd()\n    x.unknown()\n')
        g = self.analyze()
        dest = Path(self.tmp.name) / 'out'
        write_call_statuses(g, dest)
        write_html(g, dest / 'report.html')
        exported = json.loads((dest / 'call_statuses.json').read_text(encoding='utf-8'))
        self.assertEqual(len(exported['calls']), g['total_call_sites'])
        with (dest / 'call_statuses.csv').open(encoding='utf-8-sig', newline='') as f:
            self.assertEqual(len(list(csv.DictReader(f))), g['total_call_sites'])
        html = (dest / 'report.html').read_text(encoding='utf-8')
        self.assertIn('All call statuses', html)
        self.assertIn('statusBars', html)


if __name__ == '__main__':
    unittest.main()
