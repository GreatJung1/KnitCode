"""Straight-line rebinding of a function name at module definition time."""
import tempfile
import unittest
from pathlib import Path

from ast_parser import scan_repository
from symbol_builder import build_symbol_result
from graph_builder import build_graph


class DuplicateFunctionBindingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / 'project'
        self.root.mkdir()

    def analyze(self, code):
        (self.root / 'demo.py').write_text(code, encoding='utf-8')
        facts = scan_repository(self.root)
        self.assertFalse(facts['parse_errors'])
        symbols = build_symbol_result(facts)
        graph = build_graph(symbols)
        return symbols, graph

    @staticmethod
    def edges_on_line(graph, line, callee):
        return [e for e in graph['edges'] if e['type'] == 'CALLS'
                and e.get('evidence', {}).get('line') == line
                and e.get('evidence', {}).get('source') == callee]

    def test_decorator_uses_first_definition_then_second_and_no_future(self):
        symbols, graph = self.analyze('''\
helper()  # before any definition: must not guess a future definition

def helper():
    pass

def dec(value):
    return lambda fn: fn

@dec(helper())
def first_test():
    pass

def helper():
    pass

@dec(helper())
def second_test():
    pass
''')
        definitions = sorted((s for s in symbols['symbols']
                              if s['canonical_name'] == 'demo.helper'),
                             key=lambda s: s['line'])
        self.assertEqual(len(definitions), 2)
        self.assertNotEqual(definitions[0]['id'], definitions[1]['id'])
        self.assertEqual([s['line'] for s in definitions], [3, 13])
        self.assertFalse(self.edges_on_line(graph, 1, 'helper()'))
        self.assertTrue(any(c['line'] == 1 for c in graph['unresolved_calls']))
        early = self.edges_on_line(graph, 9, 'helper()')
        late = self.edges_on_line(graph, 16, 'helper()')
        self.assertEqual([(e['source'], e['target']) for e in early],
                         [('MODULE:demo', definitions[0]['id'])])
        self.assertEqual([(e['source'], e['target']) for e in late],
                         [('MODULE:demo', definitions[1]['id'])])
        self.assertEqual(graph['resolved_call_sites'] + graph['unresolved_call_sites'],
                         graph['total_call_sites'])

    def test_redefinition_default_argument_uses_previous_binding(self):
        symbols, graph = self.analyze('''\
def helper():
    pass

def helper(value=helper()):
    pass
''')
        first = min((s for s in symbols['symbols'] if s['canonical_name'] == 'demo.helper'),
                    key=lambda s: s['line'])
        edges = self.edges_on_line(graph, 4, 'helper()')
        self.assertEqual([(e['source'], e['target']) for e in edges],
                         [('MODULE:demo', first['id'])])

    def test_function_body_owns_its_own_calls_even_if_redefined(self):
        symbols, graph = self.analyze('''\
def ping():
    pass

def helper():
    ping()

def helper():
    ping()
''')
        versions = sorted((s for s in symbols['symbols']
                           if s['canonical_name'] == 'demo.helper'),
                          key=lambda s: s['line'])
        self.assertEqual(len(versions), 2)
        self.assertEqual([(e['source'], e['target']) for e in self.edges_on_line(graph, 5, 'ping()')],
                         [(versions[0]['id'], 'FUNCTION:demo.ping')])
        self.assertEqual([(e['source'], e['target']) for e in self.edges_on_line(graph, 8, 'ping()')],
                         [(versions[1]['id'], 'FUNCTION:demo.ping')])

    def test_deferred_call_not_rejected_due_to_its_source_line(self):
        _, graph = self.analyze('''\
def run():
    helper()

def helper():
    pass
''')
        self.assertEqual([(e['source'], e['target']) for e in self.edges_on_line(graph, 2, 'helper()')],
                         [('FUNCTION:demo.run', 'FUNCTION:demo.helper')])


if __name__ == '__main__':
    unittest.main()
