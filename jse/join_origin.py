"""Evidence-based identification of a narrow set of non-project ``join`` calls.

No target module is imported or executed. A verified lexical origin is not a
proof of runtime dispatch, especially if code monkeypatches class attributes.
"""
from __future__ import annotations

import ast


def literal_join_receiver(call: dict) -> str | None:
    """Return str/bytes only for a direct call on a source literal.

    Crucially, ``"x".join(seq).strip()`` is NOT a literal-receiver ``strip``.
    """
    source = call.get('source')
    if not source:
        return None
    try:
        expr = ast.parse(source, mode='eval').body
    except (SyntaxError, TypeError, ValueError):
        return None
    if not isinstance(expr, ast.Call) or not isinstance(expr.func, ast.Attribute):
        return None
    if expr.func.attr != 'join':
        return None
    value = expr.func.value
    if isinstance(value, ast.Constant):
        if isinstance(value.value, str):
            return 'str'
        if isinstance(value.value, bytes):
            return 'bytes'
    if isinstance(value, ast.JoinedStr):
        return 'str'
    return None
