"""Execution owner is distinct from lexical name scope (no target code execution)."""
import tempfile
import unittest
from pathlib import Path

from ast_parser import scan_repository
from symbol_builder import build_symbol_result
from graph_builder import build_graph


class ExecutionContextTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / 'repository'
        self.root.mkdir()

    def analyze(self, code):
        (self.root / 'demo.py').write_text(code, encoding='utf-8')
        facts = scan_repository(self.root)
        self.assertFalse(facts['parse_errors'])
        symbols = build_symbol_result(facts)
        graph = build_graph(symbols)
        return facts, symbols, graph

    def edges(self, graph, source):
        return [e for e in graph['edges'] if e['type'] == 'CALLS'
                and e.get('evidence', {}).get('source') == source]

    def test_module_decorator_call_is_module_owned_not_function_body(self):
        _, symbols, graph = self.analyze('''\
def helper(): pass
def decorate(x): return lambda f: f
@decorate(helper())
def cli():
    helper()
''')
        outer = self.edges(graph, 'decorate(helper())')
        nested = self.edges(graph, 'helper()')
        self.assertEqual(len(outer), 1)
        self.assertEqual(outer[0]['source'], 'MODULE:demo')
        self.assertEqual(outer[0]['target'], 'FUNCTION:demo.decorate')
        self.assertEqual(outer[0]['evidence']['execution_phase'], 'definition_decorator')
        self.assertEqual({(e['source'], e['target']) for e in nested}, {
            ('MODULE:demo', 'FUNCTION:demo.helper'),
            ('FUNCTION:demo.cli', 'FUNCTION:demo.helper'),
        })

    def test_nested_decorator_is_owned_by_outer_not_nested_function(self):
        _, _, graph = self.analyze('''\
def decorate(f): return f
def outer():
    @decorate
    def nested(): pass
    @decorate()
    def inner(): pass
''')
        edges = self.edges(graph, 'decorate()')
        self.assertEqual([(e['source'], e['target']) for e in edges],
                         [('FUNCTION:demo.outer', 'FUNCTION:demo.decorate')])

    def test_default_arguments_in_enclosing_context(self):
        _, _, graph = self.analyze('''\
def helper(): pass
def outer():
    def inner(x=helper()):
        pass
''')
        edges = self.edges(graph, 'helper()')
        self.assertEqual([(e['source'], e['target']) for e in edges],
                         [('FUNCTION:demo.outer', 'FUNCTION:demo.helper')])
        self.assertEqual(edges[0]['evidence']['execution_phase'], 'definition_default')

    def test_class_header_body_method_default_are_module_owned(self):
        _, _, graph = self.analyze('''\
def base(): return object
def register(x=None): return x
def helper(): pass
@register(helper())
class C(base()):
    value = helper()
    def method(self, x=helper()):
        helper()
''')
        for e in self.edges(graph, 'helper()'):
            if e['evidence']['execution_phase'] == 'function_body':
                self.assertEqual(e['source'], 'METHOD:demo.C.method')
            else:
                self.assertEqual(e['source'], 'MODULE:demo')
        self.assertEqual(len(self.edges(graph, 'helper()')), 4)
        self.assertEqual(self.edges(graph, 'base()')[0]['source'], 'MODULE:demo')
        self.assertEqual(self.edges(graph, 'register(helper())')[0]['source'], 'MODULE:demo')

    def test_nested_class_body_owned_by_enclosing_function(self):
        _, _, graph = self.analyze('''\
def helper(): pass
def outer():
    class C:
        value = helper()
''')
        self.assertEqual(self.edges(graph, 'helper()')[0]['source'], 'FUNCTION:demo.outer')

    def test_lambda_body_uses_synthetic_node_not_creator(self):
        _, symbols, graph = self.analyze('''\
def helper(): pass
def outer():
    task = lambda: helper()
    return task
''')
        lambdas = [s for s in symbols['symbols'] if s['kind'] == 'LAMBDA']
        self.assertEqual(len(lambdas), 1)
        self.assertEqual(lambdas[0]['scope'], 'outer')
        edges = self.edges(graph, 'helper()')
        self.assertEqual(len(edges), 1)
        self.assertEqual((edges[0]['source'], edges[0]['target']),
                         (lambdas[0]['id'], 'FUNCTION:demo.helper'))
        self.assertEqual(edges[0]['evidence']['execution_phase'], 'lambda_body')
        self.assertNotEqual(edges[0]['source'], 'FUNCTION:demo.outer')

    def test_lambda_defaults_now_body_later(self):
        _, symbols, graph = self.analyze('''\
def helper(): pass
def outer():
    return lambda x=helper(): helper()
''')
        lambdas = [s for s in symbols['symbols'] if s['kind'] == 'LAMBDA']
        edges = self.edges(graph, 'helper()')
        self.assertEqual(len(edges), 2)
        phases = {e['evidence']['execution_phase']: e['source'] for e in edges}
        self.assertEqual(phases, {
            'lambda_default': 'FUNCTION:demo.outer',
            'lambda_body': lambdas[0]['id'],
        })

    def test_lambda_parameter_is_not_project_function(self):
        _, _, graph = self.analyze('''\
def cb(): pass
def outer():
    return lambda cb: cb()
''')
        self.assertEqual(self.edges(graph, 'cb()'), [])

    def test_unique_lambda_ids_on_same_line(self):
        _, symbols, graph = self.analyze('''\
def a(): pass
def b(): pass
f, g = lambda: a(), lambda: b()
''')
        lambdas = [s['id'] for s in symbols['symbols'] if s['kind'] == 'LAMBDA']
        self.assertEqual(len(lambdas), 2)
        self.assertEqual(len(set(lambdas)), 2)
        self.assertIn(self.edges(graph, 'a()')[0]['source'], lambdas)
        self.assertIn(self.edges(graph, 'b()')[0]['source'], lambdas)
        self.assertNotEqual(self.edges(graph, 'a()')[0]['source'],
                            self.edges(graph, 'b()')[0]['source'])

    def test_function_calls_remain_function_owned(self):
        _, _, graph = self.analyze('''\
def helper(): pass
def f(): helper()
''')
        self.assertEqual(self.edges(graph, 'helper()')[0]['source'], 'FUNCTION:demo.f')


if __name__ == '__main__':
    unittest.main()
