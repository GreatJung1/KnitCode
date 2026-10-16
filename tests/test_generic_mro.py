"""Regression tests: source-backed generic bases and trusted stdlib MRO nodes."""
import tempfile
import textwrap
import unittest
from pathlib import Path

from ast_parser import scan_repository
from symbol_builder import build_symbol_result
from graph_builder import Resolver, build_graph
from unresolved_classifier import reclassify_graph
from call_status import annotate_call_statuses


class GenericMROTests(unittest.TestCase):
    def analyze(self, source):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / 'repo'
            root.mkdir()
            (root / 'app.py').write_text(textwrap.dedent(source), encoding='utf-8')
            data = build_symbol_result(scan_repository(root))
        graph = annotate_call_statuses(reclassify_graph(build_graph(data), data), data)
        return Resolver(data), graph

    def targets(self, graph, callee):
        sites = [r for r in graph['call_statuses'] if r['callee_text'] == callee]
        self.assertEqual(len(sites), 1, sites)
        return {e['target'] for e in graph['edges']
                if e['type'] == 'CALLS' and e['evidence'].get('call_id') == sites[0]['call_id']}

    def test_generic_base_self_method_and_super(self):
        source = '''
            import abc
            import typing as t
            T = t.TypeVar("T")
            class Base(t.Generic[T], abc.ABC):
                def fail(self): pass
                def to_info_dict(self): return {}
            class Child(Base[T], t.Generic[T]):
                def convert(self): self.fail()
                def to_info_dict(self): return super().to_info_dict()
        '''
        resolver, graph = self.analyze(source)
        order, reason = resolver._mro(resolver.classes['app.Child'])
        self.assertEqual(reason, 'complete')
        self.assertEqual(order, ['app.Child', 'app.Base', 'typing.Generic', 'abc.ABC', 'builtins.object'])
        self.assertEqual(self.targets(graph, 'self.fail'), {'METHOD:app.Base.fail'})
        self.assertEqual(self.targets(graph, 'super().to_info_dict'), {'METHOD:app.Base.to_info_dict'})

    def test_plain_non_generic_subscription_stays_unknown(self):
        _, graph = self.analyze('''
            class Base:
                def run(self): pass
            class Child(Base[int]):
                def go(self): self.run()
        ''')
        self.assertEqual(self.targets(graph, 'self.run'), set())

    def test_custom_class_getitem_returning_other_base_stays_unknown(self):
        _, graph = self.analyze('''
            import typing as t
            T = t.TypeVar("T")
            class Other:
                def run(self): pass
            class Base(t.Generic[T]):
                @classmethod
                def __class_getitem__(cls, arg):
                    return Other
                def run(self): pass
            class Child(Base[int]):
                def go(self): self.run()
        ''')
        self.assertEqual(self.targets(graph, 'self.run'), set())

    def test_rebound_typing_alias_stays_unknown(self):
        _, graph = self.analyze('''
            import typing as t
            t = unknown()
            class Base(t.Generic[int]):
                def run(self): pass
            class Child(Base[int]):
                def go(self): self.run()
        ''')
        self.assertEqual(self.targets(graph, 'self.run'), set())

    def test_opaque_multiple_base_stays_unknown(self):
        _, graph = self.analyze('''
            import abc
            import typing as t
            T=t.TypeVar("T")
            class Base(t.Generic[T], abc.ABC):
                def run(self): pass
            class Child(ExternalBase, Base[T]):
                def go(self): self.run()
        ''')
        self.assertEqual(self.targets(graph, 'self.run'), set())

    def test_stdlib_pathlike_union_type_argument(self):
        _, graph = self.analyze("""
            import os
            import abc
            import typing as t
            T=t.TypeVar('T')
            class Base(t.Generic[T],abc.ABC):
                def fail(self): pass
            class Child(Base[str | os.PathLike[str]]):
                def run(self): self.fail()
        """)
        self.assertEqual(self.targets(graph, 'self.fail'), {'METHOD:app.Base.fail'})

    def test_overloaded_union_argument_stays_unknown(self):
        _, graph = self.analyze("""
            import typing as t
            T=t.TypeVar('T')
            class Strange:
                def __or__(self, other): return t.Any
            value = Strange()
            class Base(t.Generic[T]):
                def fail(self): pass
            class Child(Base[value | str]):
                def run(self): self.fail()
        """)
        self.assertEqual(self.targets(graph, 'self.fail'), set())

    def test_metaclass_customization_stays_unknown(self):
        _, graph = self.analyze('''
            import typing as t
            T=t.TypeVar("T")
            class Base(t.Generic[T]):
                def run(self): pass
            class Child(Base[T], metaclass=SomeMeta):
                def go(self): self.run()
        ''')
        self.assertEqual(self.targets(graph, 'self.run'), set())


    def test_dynamic_type_argument_stays_unknown(self):
        _, graph = self.analyze('''
            import typing as t
            T=t.TypeVar("T")
            class Base(t.Generic[T]):
                def run(self): pass
            class Child(Base[side_effect()]):
                def go(self): self.run()
        ''')
        self.assertEqual(self.targets(graph, 'self.run'), set())

    def test_conditional_generic_import_stays_unknown(self):
        _, graph = self.analyze("""
            if condition:
                import typing as t
            class Base(t.Generic[int]):
                def run(self): pass
            class Child(Base[int]):
                def go(self): self.run()
        """)
        self.assertEqual(self.targets(graph, 'self.run'), set())

    def test_trusted_from_import_form(self):
        resolver, graph = self.analyze("""
            from abc import ABC
            from typing import Generic, TypeVar
            T=TypeVar('T')
            class Base(Generic[T],ABC):
                def run(self): pass
            class Child(Base[int]):
                def go(self): self.run()
        """)
        self.assertEqual(resolver._mro(resolver.classes['app.Child'])[1], 'complete')
        self.assertEqual(self.targets(graph, 'self.run'), {'METHOD:app.Base.run'})

    def test_patched_generic_attribute_stays_unknown(self):
        _, graph = self.analyze('''
            import typing as t
            t.Generic = opaque()
            class Base(t.Generic[int]):
                def run(self): pass
            class Child(Base[int]):
                def go(self): self.run()
        ''')
        self.assertEqual(self.targets(graph, 'self.run'), set())


if __name__ == '__main__':
    unittest.main()
