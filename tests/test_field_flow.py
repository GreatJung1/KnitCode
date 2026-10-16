import unittest
import test_object_flow as helpers
CLASSES = helpers.CLASSES


class FieldTests(unittest.TestCase):
    graph = helpers.ReturnTests.graph
    targets = helpers.ReturnTests.targets
    def test_direct_field(self):
        targets, _ = self.targets(CLASSES + 'class C:\n    def __init__(self): self.worker=A()\n    def go(self): self.worker.run()\n', 'self.worker.run')
        self.assertEqual(targets, {'METHOD:app.A.run'})

    def test_factory_field(self):
        targets, _ = self.targets(CLASSES + 'def make(): return A()\nclass C:\n    def __init__(self): self.worker=make()\n    def go(self): self.worker.run()\n', 'self.worker.run')
        self.assertEqual(targets, {'METHOD:app.A.run'})

    def test_field_reassignment(self):
        targets, _ = self.targets(CLASSES + 'class C:\n    def go(self):\n        self.worker=A()\n        self.worker=B()\n        self.worker.run()\n', 'self.worker.run')
        self.assertEqual(targets, {'METHOD:app.B.run'})

    def test_field_unknown_kills_type(self):
        targets, _ = self.targets(CLASSES + 'class C:\n    def go(self):\n        self.worker=A()\n        self.worker=unknown()\n        self.worker.run()\n', 'self.worker.run')
        self.assertEqual(targets, set())

    def test_inherited_field(self):
        targets, _ = self.targets(CLASSES + 'class Base:\n    def __init__(self): self.worker=A()\nclass Child(Base):\n    def go(self): self.worker.run()\n', 'self.worker.run')
        self.assertEqual(targets, {'METHOD:app.A.run'})

    def test_separate_instances(self):
        targets, _ = self.targets(CLASSES + 'class C: pass\ndef go():\n    x=C(); y=C()\n    x.worker=A(); y.worker=B()\n    x.worker.run()\n', 'x.worker.run')
        self.assertEqual(targets, {'METHOD:app.A.run'})

    def test_alias_updates_same_instance(self):
        targets, _ = self.targets(CLASSES + 'class C: pass\ndef go():\n    x=C(); y=x\n    x.worker=A(); y.worker=B()\n    x.worker.run()\n', 'x.worker.run')
        self.assertEqual(targets, {'METHOD:app.B.run'})

    def test_wrapped_factory_allocations_do_not_alias(self):
        source = CLASSES + '''class C: pass
def make():
    obj=C(); obj.worker=A(); return obj
def wrap(): return make()
def go():
    x=wrap(); y=wrap()
    y.worker=B()
    x.worker.run()
'''
        targets, _ = self.targets(source, 'x.worker.run')
        self.assertEqual(targets, {'METHOD:app.A.run'})

    def test_returned_object_fields(self):
        targets, _ = self.targets(CLASSES + 'class C: pass\ndef make():\n    c=C(); c.worker=A(); return c\ndef go():\n    x=make(); x.worker.run()\n', 'x.worker.run')
        self.assertEqual(targets, {'METHOD:app.A.run'})

    def test_unknown_escape_invalidates_fields(self):
        targets, _ = self.targets(CLASSES + 'class C: pass\ndef go():\n    x=C(); x.worker=A(); external(x); x.worker.run()\n', 'x.worker.run')
        self.assertEqual(targets, set())

    def test_branch_field_join(self):
        targets, _ = self.targets(CLASSES + 'class C:\n    def go(self, flag):\n        if flag: self.worker=A()\n        else: self.worker=B()\n        self.worker.run()\n', 'self.worker.run')
        self.assertEqual(targets, {'METHOD:app.A.run', 'METHOD:app.B.run'})

    def test_cross_method_mutation(self):
        targets, _ = self.targets(CLASSES + 'class C:\n    def set(self): self.worker=B()\n    def go(self):\n        self.worker=A(); self.set(); self.worker.run()\n', 'self.worker.run')
        self.assertEqual(targets, {'METHOD:app.B.run'})

    def test_return_path_heap_effects_are_joined(self):
        source = CLASSES + '''class C: pass
def make(flag):
    obj=C()
    if flag:
        obj.worker=A()
        return obj
    obj.worker=B()
    return obj
def go(flag):
    obj=make(flag)
    obj.worker.run()
'''
        targets, _ = self.targets(source, 'obj.worker.run')
        self.assertEqual(targets, {'METHOD:app.A.run', 'METHOD:app.B.run'})


