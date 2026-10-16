"""v2.2 regression tests: no target execution, conservative may-call semantics."""
import tempfile
import unittest
from pathlib import Path

from ast_parser import scan_repository
from symbol_builder import build_symbol_result
from graph_builder import build_graph
from unresolved_classifier import reclassify_graph


class V22Integration(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.root = Path(self.dir.name) / 'sample'
        self.root.mkdir()

    def run_source(self, text):
        (self.root / 'example.py').write_text(text, encoding='utf-8')
        symbols = build_symbol_result(scan_repository(self.root))
        return reclassify_graph(build_graph(symbols), symbols)

    @staticmethod
    def edges(graph, callee):
        return [e for e in graph['edges'] if e.get('evidence', {}).get('source', '').startswith(callee)]

    def test_module_edge_inside_main_guard(self):
        g = self.run_source('def hello(): pass\nif __name__ == "__main__":\n    hello()\n')
        e = self.edges(g, 'hello()')
        self.assertEqual([(x['source'], x['target'], x['type']) for x in e],
                         [('MODULE:example', 'FUNCTION:example.hello', 'CALLS')])
        self.assertTrue(e[0]['evidence']['module_level'])
        self.assertEqual(g['resolved_call_sites'], 1)
        self.assertEqual(g['unresolved_call_sites'], 0)

    def test_unrelated_module_and_external_remain_unresolved(self):
        g = self.run_source('from setuptools import setup\nsetup()\nmissing()\n')
        self.assertFalse(any(e['type'] == 'CALLS' for e in g['edges']))
        self.assertEqual(g['unresolved_call_sites'], 2)
        self.assertEqual({x['scope_context'] for x in g['unresolved_calls']}, {'MODULE_LEVEL'})
        self.assertEqual(len([n for n in g['nodes'] if n['kind'] == 'MODULE']), 1)

    def test_module_reassignment_not_false_local(self):
        g = self.run_source('def hello(): pass\nhello = fake\nhello()\n')
        self.assertFalse(any(e['target'] == 'FUNCTION:example.hello' for e in g['edges']))

    def test_module_call_before_definition_not_guessed(self):
        g = self.run_source('hello()\ndef hello(): pass\n')
        self.assertFalse(any(e['target'] == 'FUNCTION:example.hello' for e in g['edges']))
        self.assertEqual(g['unresolved_call_sites'], 1)

    def test_simple_class_list_comprehension(self):
        g = self.run_source('''class A: pass
class B: pass
def run():
    choices = [A, B]
    objects = [cls() for cls in choices]
''')
        edges = self.edges(g, 'cls()')
        targets = {e['target'] for e in edges if e['type'] == 'INSTANTIATES'}
        self.assertEqual(targets, {'CLASS:example.A', 'CLASS:example.B'})
        self.assertTrue(any(x['callee_text'] == 'cls' and len(x['targets']) == 2
                            for x in g['ambiguous_calls']))
        self.assertEqual({e['evidence']['certainty'] for e in edges}, {'multiple_possible_targets'})

    def test_dict_lookup_append_branch_comprehension(self):
        # Faithful shape of the Sublist3r main() 930-947 path, not its actual source.
        g = self.run_source('''class BaiduEnum: pass
class GoogleEnum: pass
class YahooEnum: pass
def run(engines):
    supported_engines = {'baidu': BaiduEnum, 'google': GoogleEnum, 'yahoo': YahooEnum}
    chosenEnums = []
    if engines is None:
        chosenEnums = [BaiduEnum, GoogleEnum, YahooEnum]
    else:
        engines = engines.split(',')
        for engine in engines:
            if engine.lower() in supported_engines:
                chosenEnums.append(supported_engines[engine.lower()])
    enums = [enum(1) for enum in chosenEnums]
    for enum in enums:
        enum.start()
''')
        matches = self.edges(g, 'enum(1)')
        targets = {e['target'] for e in matches if e['type'] == 'INSTANTIATES'}
        self.assertEqual(targets, {'CLASS:example.BaiduEnum', 'CLASS:example.GoogleEnum',
                                   'CLASS:example.YahooEnum'})
        self.assertEqual(len({e['evidence']['call_id'] for e in matches}), 1)
        self.assertTrue(any(x['callee_text'] == 'enum' and len(x['targets']) == 3
                            for x in g['ambiguous_calls']))
        # start is inherited from an external Thread superclass, not in this repo.
        self.assertIn('enum.start', [x['callee_text'] for x in g['unresolved_calls']])

    def test_unknown_dictionary_value_does_not_fabricate(self):
        g = self.run_source('''class A: pass
def run(external):
    table = {'a': A, 'b': external}
    choices = []
    choices.append(table[external])
    ys = [cls() for cls in choices]
''')
        matches = self.edges(g, 'cls()')
        self.assertEqual({e['target'] for e in matches if e['type'] == 'INSTANTIATES'},
                         {'CLASS:example.A'})
        self.assertTrue(all(e['evidence']['certainty'] == 'conditional_may_call' for e in matches))

    def test_overwrite_kills_stale_collection_information(self):
        g = self.run_source('''class A: pass
def run(unknown):
    choices = [A]
    choices = unknown
    ys = [cls() for cls in choices]
''')
        self.assertFalse(self.edges(g, 'cls()'))

    def test_unsupported_list_mutation_invalidates_candidates(self):
        g = self.run_source('''class A: pass
def run():
    choices = [A]
    choices.clear()
    ys = [cls() for cls in choices]
''')
        self.assertFalse(self.edges(g, 'cls()'))

    def test_comprehension_local_does_not_leak(self):
        g = self.run_source('''class A: pass
def run():
    choices = [A]
    ys = [cls() for cls in choices]
    cls()
''')
        calls = [(e['target'], e['evidence']['line']) for e in self.edges(g, 'cls()')]
        self.assertEqual(len(calls), 1)

    def test_dict_constant_key_filters_classes(self):
        g = self.run_source('''class A: pass
class B: pass
def run():
    table = {'a': A, 'b': B}
    chosen = [table['a']]
    ys = [cls() for cls in chosen]
''')
        self.assertEqual({x['target'] for x in self.edges(g, 'cls()')}, {'CLASS:example.A'})

    def test_no_internal_guess_from_plain_iteration(self):
        g = self.run_source('''class A: pass
def run(items):
    for item in items:
        item()
''')
        self.assertFalse(self.edges(g, 'item()'))
        self.assertIn('item', [x['callee_text'] for x in g['unresolved_calls']])

    def test_definite_class_from_list_item(self):
        g = self.run_source('''class A: pass
def run():
    choices=[A]
    ys=[cls() for cls in choices]
''')
        es = self.edges(g, 'cls()')
        self.assertEqual({e['target'] for e in es}, {'CLASS:example.A'})
        self.assertTrue(all(e['evidence']['certainty'] == 'definite_within_supported_flow' for e in es))


if __name__ == '__main__':
    unittest.main()
