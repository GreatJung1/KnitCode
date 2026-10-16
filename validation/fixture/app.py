class A:
    def run(self): pass
class B(A): pass
class C(A):
    def run(self): pass
class D(B, C):
    def go(self): super().run()
class E(Unknown, A): pass
def example():
    obj = D()
    obj.run()
    other = E()
    other.run()
