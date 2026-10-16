"""Type-hint-based parameter instance calls: source evidence, no false certainty."""
import tempfile
from pathlib import Path
import unittest

from ast_parser import scan_repository
from symbol_builder import build_symbol_result
from graph_builder import build_graph
from unresolved_classifier import reclassify_graph
from call_status import annotate_call_statuses
from verify_v231 import check_status


class ParameterTypeHintTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / 'repo'
        self.pkg = self.root / 'sample'
        self.pkg.mkdir(parents=True)
        (self.pkg / '__init__.py').write_text('', encoding='utf-8')

    def write(self, name, text):
        path = self.pkg / (name + '.py')
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding='utf-8')

    def graph(self):
        source = scan_repository(self.root)
        self.assertEqual([], source['parse_errors'])
        symbols = build_symbol_result(source)
        graph = annotate_call_statuses(reclassify_graph(build_graph(symbols), symbols), symbols)
        self.assertEqual(check_status(graph, symbols), 0)
        self.assertEqual(sum(x['status']=='RESOLVED_INTERNAL' for x in graph['call_statuses']),
                         graph['resolved_call_sites'])
        return symbols, graph

    def call(self, g, callee):
        items = [x for x in g['call_statuses'] if x['callee_text'] == callee]
        self.assertEqual(len(items), 1, (callee, items))
        row = items[0]
        edges = [e for e in g['edges'] if (e.get('evidence') or {}).get('call_id') == row['call_id']
                 and e['type'] == 'CALLS']
        return row, edges

    def test_plain_annotation_yields_may_call_with_evidence(self):
        self.write('app', 'class Context:\n    def invoke(self): pass\ndef go(ctx: Context):\n    ctx.invoke()\n')
        symbols, g = self.graph()
        row, edges = self.call(g, 'ctx.invoke')
        self.assertEqual([e['target'] for e in edges], ['METHOD:sample.app.Context.invoke'])
        self.assertEqual(row['certainty'], 'MAY_CALL')
        self.assertEqual(edges[0]['evidence']['rule'], 'instance_parameter_type_hint')
        self.assertIn('annotated_parameter', str(edges[0]['evidence']['reaching_definitions']))
        fn = next(s for s in symbols['symbols'] if s['canonical_name'] == 'sample.app.go')
        self.assertEqual(fn['parameter_annotations'], {'ctx': 'Context'})

    def test_import_alias_and_forward_reference(self):
        self.write('models', 'class User:\n    def login(self): pass\n')
        self.write('app', 'from .models import User as U\ndef go(ctx: "U"):\n    ctx.login()\n')
        _, g = self.graph()
        self.assertEqual(self.call(g, 'ctx.login')[1][0]['target'], 'METHOD:sample.models.User.login')

    def test_qualified_import_annotation(self):
        self.write('models', 'class User:\n    def login(self): pass\n')
        self.write('app', 'from . import models as m\ndef go(ctx: m.User):\n    ctx.login()\n')
        _, g = self.graph()
        self.assertEqual(self.call(g, 'ctx.login')[1][0]['target'], 'METHOD:sample.models.User.login')

    def test_parameter_override_invalidates_hint(self):
        self.write('app', 'class User:\n    def run(self): pass\nclass Other:\n    def run(self): pass\ndef go(ctx: User):\n    ctx = Other()\n    ctx.run()\n')
        _, g = self.graph()
        self.assertEqual([e['target'] for e in self.call(g, 'ctx.run')[1]],
                         ['METHOD:sample.app.Other.run'])

    def test_unknown_reassignment_removes_hint(self):
        self.write('app', 'class User:\n    def run(self): pass\ndef go(ctx: User):\n    ctx = factory()\n    ctx.run()\n')
        _, g = self.graph()
        self.assertEqual(self.call(g, 'ctx.run')[1], [])
        self.assertEqual(self.call(g, 'ctx.run')[0]['status'], 'UNKNOWN')

    def test_branch_reassignment_is_may_call_not_definite(self):
        self.write('app', 'class User:\n    def run(self): pass\ndef go(ctx: User, flag):\n    if flag:\n        ctx = unknown()\n    ctx.run()\n')
        _, g = self.graph()
        row, edges = self.call(g, 'ctx.run')
        self.assertEqual([e['target'] for e in edges], ['METHOD:sample.app.User.run'])
        self.assertEqual(row['certainty'], 'MAY_CALL')
        self.assertIn('partial_type_candidates', edges[0]['evidence']['rule'])

    def test_unannotated_parameter_remains_unknown(self):
        self.write('app', 'class User:\n    def run(self): pass\ndef go(ctx):\n    ctx.run()\n')
        _, g = self.graph()
        self.assertEqual(self.call(g, 'ctx.run')[1], [])

    def test_generic_annotation_is_not_guessed(self):
        self.write('app', 'class User:\n    def run(self): pass\ndef go(ctx: User | None):\n    ctx.run()\n')
        _, g = self.graph()
        self.assertEqual(self.call(g, 'ctx.run')[1], [])

    def test_unknown_type_name_not_linked_by_method_name(self):
        self.write('app', 'class User:\n    def run(self): pass\ndef go(ctx: Unrelated):\n    ctx.run()\n')
        _, g = self.graph()
        self.assertEqual(self.call(g, 'ctx.run')[1], [])

    def test_external_annotation_not_guessed_by_method_name(self):
        self.write('app', 'from external_package import Context\nclass Local:\n    def invoke(self): pass\ndef go(ctx: Context):\n    ctx.invoke()\n')
        _, g = self.graph()
        self.assertEqual(self.call(g, 'ctx.invoke')[1], [])

    def test_branch_two_supported_classes_are_both_may_calls(self):
        self.write('app', 'class User:\n    def run(self): pass\nclass Other:\n    def run(self): pass\ndef go(ctx: User, flag):\n    if flag:\n        ctx = Other()\n    ctx.run()\n')
        _, g = self.graph()
        row, edges = self.call(g, 'ctx.run')
        self.assertEqual({e['target'] for e in edges},
                         {'METHOD:sample.app.User.run', 'METHOD:sample.app.Other.run'})
        self.assertEqual(row['certainty'], 'MAY_CALL')

    def test_direct_parameter_call_is_not_a_constructor(self):
        self.write('app', 'class User:\n    def __call__(self): pass\ndef go(ctx: User):\n    ctx()\n')
        _, g = self.graph()
        self.assertEqual(self.call(g, 'ctx')[1], [])

    def test_subclass_inherited_method_is_found(self):
        self.write('app', 'class Base:\n    def run(self): pass\nclass Child(Base): pass\ndef go(ctx: Child):\n    ctx.run()\n')
        _, g = self.graph()
        self.assertEqual([e['target'] for e in self.call(g, 'ctx.run')[1]],
                         ['METHOD:sample.app.Base.run'])

    def test_alias_of_typed_parameter_retains_hint(self):
        self.write('app', 'class User:\n    def run(self): pass\ndef go(ctx: User):\n    x = ctx\n    x.run()\n')
        _, g = self.graph()
        self.assertEqual([e['target'] for e in self.call(g, 'x.run')[1]],
                         ['METHOD:sample.app.User.run'])

    def test_del_invalidates_hint(self):
        self.write('app', 'class User:\n    def run(self): pass\ndef go(ctx: User):\n    del ctx\n    ctx.run()\n')
        _, g = self.graph()
        self.assertEqual(self.call(g, 'ctx.run')[1], [])

    def test_import_rebind_invalidates_hint(self):
        self.write('app', 'class User:\n    def run(self): pass\ndef go(ctx: User):\n    import pkg as ctx\n    ctx.run()\n')
        _, g = self.graph()
        self.assertEqual(self.call(g, 'ctx.run')[1], [])

    def test_async_and_keyword_only_parameter(self):
        self.write('app', 'class User:\n    def run(self): pass\nasync def go(*, ctx: User):\n    ctx.run()\n')
        _, g = self.graph()
        self.assertEqual([e['target'] for e in self.call(g, 'ctx.run')[1]],
                         ['METHOD:sample.app.User.run'])


if __name__ == '__main__':
    unittest.main()
