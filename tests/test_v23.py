"""v2.3 end-to-end regression fixtures; analyze source statically, never import it."""
import tempfile
import unittest
from pathlib import Path
from collections import Counter

from ast_parser import scan_repository
from symbol_builder import build_symbol_result
from graph_builder import build_graph
from unresolved_classifier import reclassify_graph


class V23Integration(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.root = Path(self.dir.name) / 'src'
        self.pkg = self.root / 'requests'
        self.pkg.mkdir(parents=True)
        (self.pkg / '__init__.py').write_text('', encoding='utf-8')

    def write(self, module, source):
        path = self.pkg / (module + '.py')
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(source, encoding='utf-8')

    def run_project(self):
        facts = scan_repository(self.root)
        self.assertEqual(facts['parse_errors'], [])
        sym = build_symbol_result(facts)
        graph = reclassify_graph(build_graph(sym), sym)
        self.assertEqual(graph['total_call_sites'], graph['resolved_call_sites'] + graph['unresolved_call_sites'])
        self.assertEqual(len({s['id'] for s in sym['symbols']}), len(sym['symbols']))
        self.assertEqual(len({s['id'] for s in graph['nodes']}), len(graph['nodes']))
        self.assertEqual(len({(e['source'], e['target'], e['type'], e['evidence'].get('call_id')) for e in graph['edges']}), len(graph['edges']))
        self.assertEqual(len(graph['unresolved_calls']), graph['unresolved_call_sites'])
        return sym, graph

    @staticmethod
    def resolved(g, fragment):
        return [e for e in g['edges'] if e.get('evidence', {}).get('source', '') == fragment]

    @staticmethod
    def unresolved(g, text):
        return [x for x in g['unresolved_calls'] if x['callee_text'] == text]

    def test_requests_overloads_are_type_declarations_not_runtime_nodes(self):
        self.write('auth', '''from typing import overload
class Auth:
    @overload
    def __init__(self, value: str): ...
    @overload
    def __init__(self, value: int): ...
    def __init__(self, value):
        self.value = value

def build():
    return Auth('x')
''')
        sym, g = self.run_project()
        methods = [s for s in sym['symbols'] if s['canonical_name']=='requests.auth.Auth.__init__']
        self.assertEqual(len(methods), 1)
        self.assertEqual(methods[0]['line'], 7)
        self.assertEqual(methods[0]['overload_lines'], [4, 6])
        self.assertEqual(len(sym['overload_declarations']), 1)
        self.assertEqual(len([e for e in g['edges'] if e['type']=='CONTAINS' and e['target']==methods[0]['id']]), 1)
        self.assertEqual(len([e for e in g['edges'] if e['type']=='CALLS' and e['target']==methods[0]['id']]), 1)

    def test_overload_aliases_and_only_declarations(self):
        self.write('auth', '''from typing import overload as overload_this
import typing as t
@overload_this
def f(a: str): ...
@t.overload
def f(a: int): ...
def f(a): return a

@overload_this
def no_implementation(a: str): ...
''')
        sym, g = self.run_project()
        self.assertEqual(len([s for s in sym['symbols'] if s['name']=='f']), 1)
        self.assertFalse(any(s['name']=='no_implementation' for s in sym['symbols']))
        self.assertEqual(len(sym['overload_declarations']), 2)

    def test_stdlib_reexport_is_not_internal_edge(self):
        self.write('compat', 'from urllib.parse import urlparse\nfrom builtins import str as builtin_str\n')
        self.write('adapters', 'from .compat import urlparse, builtin_str\ndef run(s):\n    urlparse(s)\n    builtin_str(s)\n')
        sym, g = self.run_project()
        self.assertEqual(g['resolved_call_sites'], 0)
        self.assertEqual([x['category'] for x in self.unresolved(g,'urlparse')], ['STDLIB'])
        self.assertEqual([x['category'] for x in self.unresolved(g,'builtin_str')], ['STDLIB'])
        self.assertTrue(any('urllib.parse' in x['classification_evidence']['imported_module'] for x in self.unresolved(g,'urlparse')))

    def test_relative_reexport_to_local_definition(self):
        self.write('utils', 'def super_len(a): return 42\n')
        self.write('bridge', 'from .utils import super_len as internal_len\n')
        self.write('adapters', 'from .bridge import internal_len\ndef go(): return internal_len(4)\n')
        _, g = self.run_project()
        self.assertEqual(len(self.resolved(g, 'internal_len(4)')), 1)
        self.assertEqual(self.resolved(g,'internal_len(4)')[0]['target'], 'FUNCTION:requests.utils.super_len')

    def test_parent_relative_import_and_reexport(self):
        (self.pkg / 'nested').mkdir()
        (self.pkg / 'nested' / '__init__.py').write_text('', encoding='utf-8')
        self.write('utils','def f(): pass\n')
        self.write('nested.reader','from ..utils import f\ndef use():\n    f()\n')
        _,g=self.run_project()
        self.assertEqual(self.resolved(g,'f()')[0]['target'],'FUNCTION:requests.utils.f')

    def test_relative_import_from_package_init(self):
        self.write('utils', 'def f(): pass\n')
        (self.pkg/'__init__.py').write_text('from .utils import f\n',encoding='utf-8')
        self.write('entry', 'from . import f\ndef start():\n    f()\n')
        _,g=self.run_project()
        self.assertEqual(self.resolved(g,'f()')[0]['target'], 'FUNCTION:requests.utils.f')

    def test_mixed_stdlib_external_conditional_reexport_is_not_guessed(self):
        self.write('compat', '''try:
    from simplejson import loads as load_json
except ImportError:
    from json import loads as load_json
''')
        self.write('api', 'from .compat import load_json\ndef run(x): return load_json(x)\n')
        _,g=self.run_project()
        self.assertEqual(self.resolved(g,'load_json(x)'), [])
        self.assertEqual(self.unresolved(g,'load_json')[0]['category'], 'DYNAMIC_UNKNOWN')
        candidates = self.unresolved(g,'load_json')[0]['classification_evidence']['origin_candidates']
        self.assertEqual({o['kind'] for o in candidates}, {'stdlib','external'})

    def test_same_name_external_import_cannot_fallback_to_project(self):
        self.write('api', 'from external import f\ndef f(): pass\ndef run(): f()\n')
        _,g=self.run_project()
        # Resolving redefined names in identical module needs Python binding
        # order. This fixture deliberately focuses on import shadowing before
        # a local function of the same name: the local def wins at call time.
        self.assertEqual(self.resolved(g,'f()')[0]['target'],'FUNCTION:requests.api.f')

    def test_reexport_late_import_shadows_original_function(self):
        self.write('api', 'def f(): pass\nfrom urllib.parse import urlparse as f\n')
        self.write('consumer', 'from .api import f\ndef run(x): return f(x)\n')
        _, g = self.run_project()
        self.assertEqual(self.resolved(g, 'f(x)'), [])
        self.assertEqual(self.unresolved(g, 'f')[0]['category'], 'STDLIB')

    def test_builtin_assignment_reexport(self):
        self.write('compat', 'builtin_str = str\n')
        self.write('consumer', 'from .compat import builtin_str\ndef run(x): return builtin_str(x)\n')
        _, g = self.run_project()
        self.assertEqual(self.resolved(g, 'builtin_str(x)'), [])
        self.assertEqual(self.unresolved(g, 'builtin_str')[0]['category'], 'BUILTIN')

    def test_outside_package_external_remains_unresolved(self):
        self.write('api', 'from httpx import get\ndef run(): get("https://example.com")\n')
        _,g=self.run_project()
        self.assertFalse(self.resolved(g,'get("https://example.com")'))
        self.assertEqual(self.unresolved(g,'get')[0]['category'],'EXTERNAL_IMPORT')


if __name__=='__main__':
    unittest.main()

# End-to-end examples above intentionally avoid importing target packages.
