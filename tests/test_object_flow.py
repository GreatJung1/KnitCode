import tempfile
import unittest
from pathlib import Path
from ast_parser import scan_repository
from symbol_builder import build_symbol_result
from graph_builder import build_graph
from unresolved_classifier import reclassify_graph
from call_status import annotate_call_statuses


CLASSES = 'class A:\n    def run(self): pass\nclass B:\n    def run(self): pass\n'


class ReturnTests(unittest.TestCase):
    def graph(self, source, extra=None):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / 'repo'
            root.mkdir()
            for name, text in {'app.py': source, **(extra or {})}.items():
                (root / name).write_text(text, encoding='utf-8')
            data = build_symbol_result(scan_repository(root))
            return annotate_call_statuses(reclassify_graph(build_graph(data), data), data)

    def targets(self, source, callee='obj.run', extra=None):
        g = self.graph(source, extra)
        rows = [r for r in g['call_statuses'] if r['callee_text'] == callee]
        self.assertEqual(len(rows), 1, rows)
        return set(rows[0]['target_ids']), rows[0]

    def check(self, factory, expected, call='obj=factory(); obj.run()', callee='obj.run'):
        targets, row = self.targets(CLASSES + factory + '\ndef go(flag):\n    ' + call + '\n', callee)
        self.assertEqual(targets, {'METHOD:app.' + name + '.run' for name in expected})
        if expected:
            self.assertEqual(row['certainty'], 'MAY_CALL')

    def test_return_constructor(self):
        self.check('def factory(): return A()\n', ['A'])

    def test_annotation_stub(self):
        self.check('def factory() -> A: ...\n', ['A'])

    def test_contradictory_annotation(self):
        self.check('def factory() -> A: return B()\n', ['B'])

    def test_annotation_does_not_override_none(self):
        self.check('def factory() -> A: return None\n', [])

    def test_annotation_does_not_override_implicit_none(self):
        self.check('def factory() -> A: pass\n', [])

    def test_custom_new_not_assumed_requested_class(self):
        self.check('class C:\n    def __new__(cls): return B()\n    def run(self): pass\ndef factory(): return C()\n', [])

    def test_branch_returns(self):
        self.check('def factory(flag):\n    if flag: return A()\n    else: return B()\n', ['A', 'B'], 'obj=factory(flag); obj.run()')

    def test_chained_return(self):
        self.check('def inner(): return A()\ndef factory(): return inner()\n', ['A'])

    def test_chained_call(self):
        self.check('def factory(): return A()\n', ['A'], 'factory().run()', 'factory().run')

    def test_none_path(self):
        self.check('def factory(flag):\n    if flag: return A()\n', ['A'], 'obj=factory(flag); obj.run()')

    def test_unknown_path(self):
        self.check('def factory(flag):\n    if flag: return A()\n    return unknown()\n', ['A'], 'obj=factory(flag); obj.run()')

    def test_reassignment(self):
        self.check('def factory(): return A()\n', [], 'obj=factory(); obj=None; obj.run()')

    def test_recursive_cutoff(self):
        self.check('def factory(): return factory()\n', [])

    def test_recursive_base_case(self):
        self.check('def factory(flag):\n    if flag: return A()\n    return factory(flag)\n', ['A'], 'obj=factory(flag); obj.run()')

    def test_mutual_recursion(self):
        self.check('def factory(): return inner()\ndef inner(): return factory()\n', [])

    def test_async_not_returned_instance(self):
        self.check('async def factory() -> A: return A()\n', [])

    def test_generator_not_returned_instance(self):
        self.check('def factory() -> A:\n    yield 1\n    return A()\n', [])

    def test_cross_file_reexport(self):
        extra = {'models.py': CLASSES + 'def create(): return A()\n', 'api.py': 'from models import create as factory\n'}
        targets, _ = self.targets('from api import factory as make\ndef go():\n    obj=make(); obj.run()\n', extra=extra)
        self.assertEqual(targets, {'METHOD:models.A.run'})

    def test_shadowed_factory(self):
        self.check('def factory(): return A()\n', [], 'factory=unknown; obj=factory(); obj.run()')

    def test_missing_required_argument(self):
        self.check('def factory(required): return A()\n', [])

    def test_bad_keyword_argument(self):
        self.check('def factory(): return A()\n', [], 'obj=factory(bad=1); obj.run()')

    def test_star_arguments_remain_unknown(self):
        self.check('def factory(): return A()\n', [], 'obj=factory(*unknown); obj.run()')


if __name__ == '__main__':
    unittest.main()
