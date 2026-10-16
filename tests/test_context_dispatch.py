import unittest
import test_object_flow as helpers


BASE = '''class Base:
    def run(self): pass
    def execute(self): self.run()
class Left(Base):
    def run(self): pass
class Right(Base):
    def run(self): pass
'''


class DispatchTests(unittest.TestCase):
    graph = helpers.ReturnTests.graph
    targets = helpers.ReturnTests.targets

    def test_inherited_body_actual_child(self):
        ts, _ = self.targets(BASE + '\ndef go():\n    x=Left(); x.execute()\n', 'self.run')
        self.assertEqual(ts, {'METHOD:app.Left.run'})

    def test_unused_subclass_not_added(self):
        ts, _ = self.targets(BASE + '\ndef go():\n    x=Base(); x.execute()\n', 'self.run')
        self.assertEqual(ts, {'METHOD:app.Base.run'})

    def test_multiple_observed_receivers(self):
        ts, row = self.targets(BASE + '\ndef go():\n    x=Left(); x.execute()\n    y=Right(); y.execute()\n', 'self.run')
        self.assertEqual(ts, {'METHOD:app.Left.run', 'METHOD:app.Right.run'})
        self.assertEqual(row['certainty'], 'MAY_CALL')

    def test_factory_receiver(self):
        ts, _ = self.targets(BASE + '\ndef make(): return Left()\ndef go():\n    x=make(); x.execute()\n', 'self.run')
        self.assertEqual(ts, {'METHOD:app.Left.run'})

    def test_receiver_through_parameter(self):
        ts, _ = self.targets(BASE + '\ndef invoke(x): x.execute()\ndef go(): invoke(Left())\n', 'self.run')
        self.assertEqual(ts, {'METHOD:app.Left.run'})

    def test_context_sensitive_return(self):
        ts, _ = self.targets(BASE + '\ndef identity(x): return x\ndef go():\n    x=identity(Left()); y=identity(Right()); x.run()\n', 'x.run')
        self.assertEqual(ts, {'METHOD:app.Left.run'})

    def test_actual_super_sibling(self):
        source = '''class A:
    def run(self): pass
class B(A):
    def run(self): super().run()
class C(A):
    def run(self): pass
class D(B,C): pass
def go():
    x=D(); x.run()
'''
        ts, _ = self.targets(source, 'super().run')
        self.assertEqual(ts, {'METHOD:app.C.run'})

    def test_super_does_not_add_unused_sibling(self):
        source = '''class A:
    def run(self): pass
class B(A):
    def run(self): super().run()
class C(A):
    def run(self): pass
class D(B,C): pass
def go():
    x=B(); x.run()
'''
        ts, _ = self.targets(source, 'super().run')
        self.assertEqual(ts, {'METHOD:app.A.run'})

    def test_subclass_field_override_in_inherited_method(self):
        source = helpers.CLASSES + '''class Parent:
    def __init__(self): self.worker=A()
    def execute(self): self.worker.run()
class Child(Parent):
    def __init__(self):
        super().__init__()
        self.worker=B()
def go():
    obj=Child(); obj.execute()
'''
        ts, _ = self.targets(source, 'self.worker.run')
        self.assertEqual(ts, {'METHOD:app.B.run'})

    def test_inherited_initialization_via_super(self):
        source = helpers.CLASSES + '''class Parent:
    def __init__(self): self.worker=A()
class Child(Parent):
    def __init__(self): super().__init__()
    def execute(self): self.worker.run()
def go():
    obj=Child(); obj.execute()
'''
        ts, _ = self.targets(source, 'self.worker.run')
        self.assertEqual(ts, {'METHOD:app.A.run'})

    def test_instance_attribute_blocks_method(self):
        ts, _ = self.targets(BASE + '\ndef go():\n    x=Left(); x.run=None; x.execute()\n', 'self.run')
        self.assertEqual(ts, set())
