from subbrute import subbrute as sb
from subbrute.subbrute import trace as say

class Base:
    def __init__(self):
        pass
    def print_(self):
        pass

class GoogleEnum(Base):
    def extract_domains(self):
        self.print_()
        super().__init__()

class AskEnum(Base):
    def extract_domains(self):
        self.print_()

def launch():
    obj = GoogleEnum()
    obj.extract_domains()
    sb.print_target()
    say()

def dispatch():
    chosenEnums = [GoogleEnum, AskEnum]
    for enum in chosenEnums:
        enum()

class Payment:
    def run(self):
        pass

class Other:
    def run(self):
        pass

def foo():
    svc = Payment()
    svc.run()

def bar():
    svc = Other()
    svc.run()

def reassigned():
    svc = Payment()
    svc.run()
    svc = Other()
    svc.run()

def branch(flag):
    if flag:
        svc = Payment()
    else:
        svc = Other()
    svc.run()  # Unresolved rather than wrongly guessed.

def shadowed_parameter(launch):
    launch()  # function parameter, not sublist3r.launch

def outer():
    def nested():
        return 1
    def inner():
        return nested()
    return inner()

# v2 reaching-definitions and control-flow regression fixtures.
def same_branch(flag):
    if flag:
        pscan = Payment()
        pscan.run()

def conditional_outside(flag):
    if flag:
        pscan = Payment()
    pscan.run()  # May be unbound if flag is false.

def branch_both_inside(flag):
    if flag:
        svc = Payment()
        svc.run()
    else:
        svc = Other()
        svc.run()

def preexisting_and_branch(flag):
    svc = Payment()
    if flag:
        svc = Other()
    svc.run()

def branch_then_override(flag):
    if flag:
        svc = Payment()
    else:
        svc = Other()
    svc = Payment()
    svc.run()

def alias_not_updated():
    svc = Payment()
    previous = svc
    svc = Other()
    previous.run()

def return_branch(flag):
    if flag:
        svc = Payment()
        return
    else:
        svc = Other()
    svc.run()

def unknown_overwrites():
    svc = Payment()
    svc = mystery()
    svc.run()

def conditional_unknown(flag):
    svc = Payment()
    if flag:
        svc = mystery()
    svc.run()  # Known possibility plus an unknown alternative.
