from models import Context, Other


def annotated(ctx: Context):
    ctx.invoke()  # v2.4 -> Context.invoke (advisory MAY_CALL)


def forward(ctx: 'Context'):
    ctx.fail()  # v2.4 -> Context.fail (advisory MAY_CALL)


def reassigned(ctx: Context):
    ctx = Other()
    ctx.invoke()  # follows assignment: Other.invoke


def uncertain(ctx: Context, condition):
    if condition:
        ctx = make_unknown()
    ctx.invoke()  # partial candidate (MAY_CALL)


def untyped(ctx):
    ctx.invoke()  # no type information; stays UNKNOWN
