"""C3 dispatch counterexamples; only these trusted fixtures run in Python."""
import tempfile
import textwrap
import unittest
from pathlib import Path

from ast_parser import scan_repository
from symbol_builder import build_symbol_result
from graph_builder import Resolver, build_graph
from unresolved_classifier import reclassify_graph
from call_status import annotate_call_statuses


DIAMOND = '''
class A:
    def run(self): pass
class B(A): pass
class C(A):
    def run(self): pass
class D(B, C): pass
'''


class C3Tests(unittest.TestCase):
    def analyze(self, source):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / 'repo'
            root.mkdir()
            (root / 'app.py').write_text(textwrap.dedent(source), encoding='utf-8')
            facts = scan_repository(root)
            self.assertEqual(facts['parse_errors'], [])
            data = build_symbol_result(facts)
        graph = annotate_call_statuses(reclassify_graph(build_graph(data), data), data)
        return data, graph

    def targets(self, graph, callee, caller=None):
        rows = [r for r in graph['call_statuses'] if r['callee_text'] == callee
                and (caller is None or r['caller_id'] == 'METHOD:app.' + caller)]
        self.assertEqual(len(rows), 1, rows)
        return {e['target'].removeprefix('METHOD:app.') for e in graph['edges']
                if e['type'] == 'CALLS' and e['evidence'].get('call_id') == rows[0]['call_id']}

    def test_diamond_matches_python(self):
        ns = {}
        exec(DIAMOND, ns)
        _, g = self.analyze(DIAMOND + '\ndef go():\n    obj = D()\n    obj.run()\n')
        self.assertEqual(self.targets(g, 'obj.run'), {ns['D'].run.__qualname__})

    def test_single_inheritance(self):
        _, g = self.analyze('class A:\n    def run(self): pass\nclass B(A): pass\ndef go():\n    obj=B()\n    obj.run()\n')
        self.assertEqual(self.targets(g, 'obj.run'), {'A.run'})

    def test_override(self):
        _, g = self.analyze(DIAMOND + '\nclass E(D):\n    def run(self): pass\ndef go():\n    obj=E()\n    obj.run()\n')
        self.assertEqual(self.targets(g, 'obj.run'), {'E.run'})

    def test_unrelated_multiple_bases_left_to_right(self):
        _, g = self.analyze('class A:\n    def run(self): pass\nclass B:\n    def run(self): pass\nclass D(A,B): pass\ndef go():\n    D.run(None)\n')
        self.assertEqual(self.targets(g, 'D.run'), {'A.run'})

    def test_constructor_uses_c3(self):
        _, g = self.analyze(DIAMOND.replace('run', '__init__') + '\ndef go():\n    D()\n')
        self.assertEqual(self.targets(g, 'D'), {'C.__init__'})

    def test_super_uses_owner_linearization(self):
        _, g = self.analyze(DIAMOND.replace('class D(B, C): pass', 'class D(B, C):\n    def go(self): super().run()'))
        self.assertEqual(self.targets(g, 'super().run'), {'C.run'})

    def test_cooperative_super_uses_subclass_mro(self):
        source = DIAMOND.replace('class B(A): pass', 'class B(A):\n    def run(self): super().run()')
        _, g = self.analyze(source)
        self.assertEqual(self.targets(g, 'super().run'), {'A.run', 'C.run'})

    def test_explicit_super_anchor(self):
        source = DIAMOND.replace('class D(B, C): pass', 'class D(B, C):\n    def go(self): super(C, self).run()')
        _, g = self.analyze(source)
        self.assertEqual(self.targets(g, 'super().run'), {'A.run'})

    def test_invalid_explicit_super_receiver(self):
        source = DIAMOND.replace('class D(B, C): pass', 'class D(B, C):\n    def go(self): super(C, 123).run()')
        _, g = self.analyze(source)
        self.assertEqual(self.targets(g, 'super().run'), set())

    def test_unknown_parent_is_not_skipped(self):
        for bases in ('Unknown, A', 'A, Unknown'):
            with self.subTest(bases=bases):
                _, g = self.analyze('class A:\n    def run(self): pass\nclass D(' + bases + '): pass\ndef go():\n    obj=D()\n    obj.run()\n')
                self.assertEqual(self.targets(g, 'obj.run'), set())
                row = next(r for r in g['call_statuses'] if r['callee_text'] == 'obj.run')
                self.assertEqual(row['status'], 'UNKNOWN')

    def test_local_override_with_unknown_parent(self):
        _, g = self.analyze('class D(Unknown):\n    def run(self): pass\ndef go():\n    obj=D()\n    obj.run()\n')
        self.assertEqual(self.targets(g, 'obj.run'), {'D.run'})

    def test_invalid_mro_not_dfs_fallback(self):
        source = 'class X:\n    def run(self): pass\nclass Y: pass\nclass A(X,Y): pass\nclass B(Y,X): pass\nclass D(A,B): pass\ndef go():\n    obj=D()\n    obj.run()\n'
        with self.assertRaises(TypeError):
            exec(source, {})
        _, g = self.analyze(source)
        self.assertEqual(self.targets(g, 'obj.run'), set())

    def test_duplicate_bases(self):
        _, g = self.analyze('class A:\n    def run(self): pass\nclass D(A,A): pass\ndef go():\n    D.run(None)\n')
        self.assertEqual(self.targets(g, 'D.run'), set())

    def test_invalid_class_has_no_instantiation_edge(self):
        _, g = self.analyze('class A: pass\nclass D(A,A): pass\ndef go(): D()\n')
        row = next(r for r in g['call_statuses'] if r['callee_text'] == 'D')
        self.assertEqual(row['target_ids'], [])
        self.assertEqual(row['status'], 'UNKNOWN')

    def test_super_local_binding_after_call(self):
        _, g = self.analyze('class A:\n    def run(self): pass\nclass B(A):\n    def go(self):\n        super().run()\n        super=other\n')
        self.assertEqual(self.targets(g, 'super().run'), set())

    def test_explicit_object_constraint(self):
        _, g = self.analyze('class A:\n    def run(self): pass\nclass D(object,A): pass\ndef go():\n    D.run(None)\n')
        self.assertEqual(self.targets(g, 'D.run'), set())

    def test_dynamic_base_not_erased(self):
        _, g = self.analyze('class A:\n    def run(self): pass\nclass D(A[int]): pass\ndef go():\n    D.run(None)\n')
        self.assertEqual(self.targets(g, 'D.run'), set())

    def test_nonmethod_attribute_shadows_parent(self):
        _, g = self.analyze('class A:\n    def run(self): pass\nclass D(A):\n    run=None\ndef go():\n    D.run(None)\n')
        self.assertEqual(self.targets(g, 'D.run'), set())

    def test_shadowed_super(self):
        _, g = self.analyze('class A:\n    def run(self): pass\nclass D(A):\n    def go(self, super): super().run()\n')
        self.assertEqual(self.targets(g, 'super().run'), set())

    def test_classmethod_super(self):
        _, g = self.analyze('class A:\n    @classmethod\n    def run(cls): pass\nclass D(A):\n    @classmethod\n    def go(cls): super().run()\n')
        self.assertEqual(self.targets(g, 'super().run'), {'A.run'})

    def test_staticmethod_zero_arg_super_is_invalid(self):
        _, g = self.analyze('class A:\n    def run(self): pass\nclass D(A):\n    @staticmethod\n    def go(): super().run()\n')
        self.assertEqual(self.targets(g, 'super().run'), set())

    def test_metaclass_not_assumed_c3(self):
        _, g = self.analyze('class A:\n    def run(self): pass\nclass D(A,metaclass=Meta): pass\ndef go():\n    D.run(None)\n')
        self.assertEqual(self.targets(g, 'D.run'), set())

    def test_linearization_matches_python(self):
        source = 'class O: pass\nclass F(O): pass\nclass E(O): pass\nclass D(O): pass\nclass C(D,F): pass\nclass B(E,D): pass\nclass A(B,C): pass\n'
        ns = {}
        exec(source, ns)
        data, _ = self.analyze(source)
        resolver = Resolver(data)
        order, reason = resolver._mro(resolver.classes['app.A'])
        self.assertEqual(reason, 'complete')
        self.assertEqual([c.rsplit('.', 1)[-1] for c in order], [c.__name__ for c in ns['A'].__mro__])

    def test_super_status_is_may_call(self):
        _, g = self.analyze('class A:\n    def run(self): pass\nclass B(A):\n    def go(self): super().run()\n')
        row = next(r for r in g['call_statuses'] if r['callee_text'] == 'super().run')
        self.assertEqual(row['certainty'], 'MAY_CALL')

    def test_rebound_receiver_not_assumed_owner(self):
        _, g = self.analyze('class A:\n    def run(self): pass\nclass B(A):\n    def go(self):\n        self=unknown()\n        super().run()\n')
        self.assertEqual(self.targets(g, 'super().run'), set())

    def test_property_blocks_inherited_method(self):
        _, g = self.analyze('class A:\n    def run(self): pass\nclass B(A):\n    @property\n    def run(self): return None\ndef go():\n    obj=B()\n    obj.run()\n')
        self.assertEqual(self.targets(g, 'obj.run'), set())

    def test_rebound_base_is_unknown(self):
        _, g = self.analyze('class A:\n    def run(self): pass\nA=unknown()\nclass B(A): pass\ndef go():\n    B.run(None)\n')
        self.assertEqual(self.targets(g, 'B.run'), set())

    def test_cycle_terminates(self):
        data, _ = self.analyze('class A: pass\nclass B(A): pass\n')
        # Corrupt/externally supplied symbol data must also terminate safely.
        resolver = Resolver(data)
        resolver.classes['app.A']['bases'] = ['B']
        resolver.classes['app.B']['file'] = 'other.py'
        self.assertEqual(resolver._mro(resolver.classes['app.A'])[1], 'invalid_cycle')

    def test_nested_class_base(self):
        _, g = self.analyze('class Outer:\n    class A:\n        def run(self): pass\n    class B(A):\n        def go(self): self.run()\n')
        self.assertEqual(self.targets(g, 'self.run'), {'Outer.A.run'})

    def test_imported_bases_and_reexport(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / 'repo'
            root.mkdir()
            (root / 'models.py').write_text(DIAMOND, encoding='utf-8')
            (root / 'api.py').write_text('from models import D as Export\n', encoding='utf-8')
            (root / 'app.py').write_text('from api import Export as D\ndef go():\n    obj=D()\n    obj.run()\n', encoding='utf-8')
            data = build_symbol_result(scan_repository(root))
            g = annotate_call_statuses(reclassify_graph(build_graph(data), data), data)
        self.assertEqual(self.targets(g, 'obj.run'), {'METHOD:models.C.run'})

    def test_many_orders_against_runtime(self):
        # Compare complete order, not just one selected method, on generated
        # ordinary hierarchies; Python's type constructor is the oracle.
        import random
        rng = random.Random(2503)
        runtime = {'builtins.object': object}
        symbols = []
        for i in range(60):
            pool = list(runtime)
            names = rng.sample(pool, min(len(pool), rng.randint(1, 3)))
            name = 'C' + str(i)
            canonical = 'app.' + name
            symbol = {'id': 'CLASS:' + canonical, 'kind': 'CLASS', 'name': name,
                      'canonical_name': canonical, 'qualified_name': name,
                      'module': 'app', 'scope': '<module>', 'file': 'app.py',
                      'line': i + 1, 'bases': [n.rsplit('.', 1)[-1] for n in names]}
            data = {'symbols': symbols + [symbol]}
            order, status = Resolver(data)._mro(symbol)
            try:
                actual = type(name, tuple(runtime[n] for n in names), {})
            except TypeError:
                self.assertTrue(status.startswith('invalid'), (names, status))
            else:
                self.assertEqual(status, 'complete')
                self.assertEqual([n.rsplit('.', 1)[-1] for n in order], [t.__name__ for t in actual.__mro__])
                runtime[canonical] = actual
                symbols.append(symbol)


if __name__ == '__main__':
    unittest.main()
