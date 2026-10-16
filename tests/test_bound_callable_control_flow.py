"""Soundness regressions for tracked callable values and structured statements."""
import unittest
import test_object_flow as helpers


class CallableAndControlFlowTests(unittest.TestCase):
    graph = helpers.ReturnTests.graph
    targets = helpers.ReturnTests.targets

    def expect(self, source, expr, expected, certainty=None):
        got, row = self.targets(source, expr)
        self.assertEqual(got, {'METHOD:app.' + name for name in expected})
        if certainty is not None:
            self.assertEqual(row['certainty'], certainty)

    def test_bound_method_alias(self):
        self.expect('class A:\n    def run(self): pass\ndef go():\n    obj=A(); fn=obj.run; fn()\n', 'fn', ['A.run'])

    def test_bound_method_keeps_old_receiver_after_rebind(self):
        self.expect('class A:\n    def run(self): pass\nclass B:\n    def run(self): pass\ndef go():\n    obj=A(); fn=obj.run; obj=B(); fn()\n', 'fn', ['A.run'])

    def test_bound_method_overwritten_with_unknown(self):
        self.expect('class A:\n    def run(self): pass\ndef go(external):\n    obj=A(); fn=obj.run; fn=external; fn()\n', 'fn', [])

    def test_bound_method_uses_original_descriptor_after_shadow(self):
        self.expect('class A:\n    def run(self): pass\ndef go():\n    obj=A(); fn=obj.run; obj.run=None; fn()\n', 'fn', ['A.run'])

    def test_shadowed_method_value_not_bound(self):
        self.expect('class A:\n    def run(self): pass\ndef go():\n    obj=A(); obj.run=None; fn=obj.run; fn()\n', 'fn', [])

    def test_alias_of_bound_method(self):
        self.expect('class A:\n    def run(self): pass\ndef go():\n    obj=A(); f=obj.run; g=f; g()\n', 'g', ['A.run'])

    def test_inherited_bound_method(self):
        self.expect('class A:\n    def run(self): pass\nclass B(A): pass\ndef go():\n    obj=B(); fn=obj.run; fn()\n', 'fn', ['A.run'])

    def test_bound_method_branches(self):
        src = ('class A:\n    def run(self): pass\nclass B:\n    def run(self): pass\n'
               'def go(flag):\n    if flag: obj=A()\n    else: obj=B()\n    fn=obj.run\n    fn()\n')
        self.expect(src, 'fn', ['A.run', 'B.run'], 'MAY_CALL')

    def test_bound_method_as_field(self):
        src = ('class A:\n    def run(self): pass\nclass C: pass\n'
               'def go():\n    obj=C(); a=A(); obj.handler=a.run; obj.handler()\n')
        self.expect(src, 'obj.handler', ['A.run'])

    def test_callable_instance(self):
        self.expect('class A:\n    def __call__(self): pass\ndef go():\n    obj=A(); obj()\n', 'obj', ['A.__call__'])

    def test_callable_instance_alias(self):
        self.expect('class A:\n    def __call__(self): pass\ndef go():\n    obj=A(); fn=obj; fn()\n', 'fn', ['A.__call__'])

    def test_callable_inherited(self):
        self.expect('class A:\n    def __call__(self): pass\nclass B(A): pass\ndef go():\n    B()()\n', 'B()', ['A.__call__'])

    def test_callable_override(self):
        self.expect('class A:\n    def __call__(self): pass\nclass B(A):\n    def __call__(self): pass\ndef go():\n    obj=B(); obj()\n', 'obj', ['B.__call__'])

    def test_callable_instance_attribute_cannot_shadow_special_lookup(self):
        self.expect('class A:\n    def __call__(self): pass\ndef go():\n    obj=A(); obj.__call__=None; obj()\n', 'obj', ['A.__call__'])

    def test_not_callable_instance(self):
        self.expect('class A: pass\ndef go():\n    obj=A(); obj()\n', 'obj', [])

    def test_unknown_parameter_not_guessed_callable(self):
        self.expect('class A:\n    def __call__(self): pass\ndef go(obj):\n    obj()\n', 'obj', [])

    def test_returned_callable_instance(self):
        self.expect('class A:\n    def __call__(self): pass\ndef factory(): return A()\ndef go():\n    x=factory(); x()\n', 'x', ['A.__call__'])

    def test_try_body_method(self):
        self.expect('class A:\n    def fail(self): pass\n    def go(self):\n        try:\n            self.fail()\n        except ValueError:\n            pass\n', 'self.fail', ['A.fail'])

    def test_try_handler_method(self):
        self.expect('class A:\n    def fail(self): pass\n    def go(self):\n        try:\n            pass\n        except ValueError:\n            self.fail()\n', 'self.fail', ['A.fail'])

    def test_try_finally_method(self):
        self.expect('class A:\n    def fail(self): pass\n    def go(self):\n        try:\n            pass\n        finally:\n            self.fail()\n', 'self.fail', ['A.fail'])

    def test_for_body_with_known_instances(self):
        src = ('class A:\n    def run(self): pass\nclass B:\n    def run(self): pass\n'
               'def go():\n    for obj in [A(), B()]:\n        obj.run()\n')
        self.expect(src, 'obj.run', ['A.run', 'B.run'], 'MAY_CALL')

    def test_while_body(self):
        src = 'class A:\n    def run(self): pass\ndef go(flag):\n    obj=A()\n    while flag:\n        obj.run()\n        break\n'
        self.expect(src, 'obj.run', ['A.run'])

    def test_with_body(self):
        src = 'class A:\n    def run(self): pass\ndef go(cm):\n    obj=A()\n    with cm:\n        obj.run()\n'
        self.expect(src, 'obj.run', ['A.run'])

class ConservativeControlFlowTests(unittest.TestCase):
    graph = helpers.ReturnTests.graph
    targets = helpers.ReturnTests.targets
    expect = CallableAndControlFlowTests.expect
    def test_property_value_is_not_a_bound_method(self):
        src = ('class A:\n    @property\n    def prop(self): return 3\n'
               'def go():\n    obj=A(); fn=obj.prop; fn()\n')
        self.expect(src, 'fn', [])

    def test_classmethod_alias_preserves_class_target(self):
        src = ('class A:\n    @classmethod\n    def build(cls): return cls()\n'
               '    def run(self): pass\ndef go():\n    obj=A(); fn=obj.build; x=fn(); x.run()\n')
        self.expect(src, 'fn', ['A.build'])
        self.expect(src, 'x.run', ['A.run'])


class BudgetFairnessTests(unittest.TestCase):
    graph = helpers.ReturnTests.graph

    def test_late_callable_is_not_starved_by_earlier_large_function(self):
        source = ('class Handler:\n    def __call__(self): pass\n'
                  'def noisy():\n' + '    object()\n' * 2200
                  + 'def late():\n    handler=Handler(); handler()\n')
        graph = self.graph(source)
        matches = [r for r in graph['call_statuses'] if r['callee_text'] == 'handler']
        self.assertEqual(len(matches), 1)
        self.assertEqual(matches[0]['target_ids'], ['METHOD:app.Handler.__call__'])
