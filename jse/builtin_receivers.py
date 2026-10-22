"""Conservative static inference for built-in receiver method calls.

Only literals and flow snapshots known to be built-in objects are accepted.
Target project code is never imported or executed. A method name alone is never
considered proof: user-defined objects can also define ``join``/``append``.
"""
from __future__ import annotations
import ast

# Literal/primitive method availability is version-specific; this limited
# allowlist is intentionally narrower than Python's entire built-in API.
METHODS = {
    'str': {'join', 'split', 'rsplit', 'splitlines', 'strip', 'lstrip', 'rstrip', 'lower', 'upper',
            'replace', 'startswith', 'endswith', 'find', 'rfind', 'format', 'encode',
            'partition', 'rpartition', 'count', 'index', 'title', 'capitalize'},
    'bytes': {'join', 'split', 'strip', 'decode', 'replace', 'startswith', 'endswith', 'find'},
    'list': {'append', 'extend', 'insert', 'pop', 'remove', 'clear', 'sort', 'reverse', 'copy', 'count', 'index'},
    'dict': {'get', 'setdefault', 'update', 'items', 'keys', 'values', 'pop', 'popitem', 'clear', 'copy'},
    'set': {'add', 'update', 'discard', 'remove', 'union', 'intersection', 'clear', 'copy', 'pop'},
    'tuple': {'count', 'index'},
}


def _type_of(expr: ast.AST) -> str | None:
    if isinstance(expr, ast.Constant):
        if isinstance(expr.value, str): return 'str'
        if isinstance(expr.value, bytes): return 'bytes'
    if isinstance(expr, ast.JoinedStr): return 'str'
    if isinstance(expr, (ast.List, ast.ListComp)): return 'list'
    if isinstance(expr, ast.Tuple): return 'tuple'
    if isinstance(expr, (ast.Set, ast.SetComp)): return 'set'
    if isinstance(expr, (ast.Dict, ast.DictComp)): return 'dict'
    # Selected immutable, well-defined return types; no arbitrary chaining.
    if (isinstance(expr, ast.Call) and isinstance(expr.func, ast.Attribute)
            and expr.func.attr in ('join', 'strip', 'lstrip', 'rstrip', 'replace', 'lower', 'upper', 'format', 'title')):
        base = _type_of(expr.func.value)
        if base in ('str', 'bytes') and expr.func.attr in METHODS[base]:
            return base
    return None


def builtin_method_origin(call: dict, *, is_unshadowed_builtin=None, has_receiver_override=None):
    """Return (type, rule) only with definite local source-backed receiver.

    builtin constructor names must be unshadowed. Name variables use reaching
    flow states; unknown/conditional/other type candidates invalidate proof.
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
    method = expr.func.attr
    rec = expr.func.value
    kind = _type_of(rec)
    rule = 'LITERAL_BUILTIN_METHOD'
    if kind is None and isinstance(rec, ast.Name):
        var = rec.id
        flow = call.get('flow') or {}
        binding = flow.get('binding') or {}
        if flow.get('variable') != var or binding.get('may_be_unbound'):
            return None
        if has_receiver_override is not None and has_receiver_override(var, method):
            return None
        options = binding.get('options') or []
        if not options:
            # Empty collections have no elements but retain a known type.
            kind = binding.get('collection')
        else:
            kinds = set()
            for opt in options:
                optkind = opt.get('kind')
                if optkind == 'builtin_instance':
                    kinds.add(opt.get('name'))
                elif optkind == 'constructed' and opt.get('name') in METHODS:
                    name = opt['name']
                    if is_unshadowed_builtin is None or not is_unshadowed_builtin(name):
                        return None
                    kinds.add(name)
                elif (binding.get('collection') in METHODS and optkind.startswith(('sequence_', 'dictionary_'))):
                    kinds.add(binding['collection'])
                else:
                    return None
            if binding.get('collection'):
                kinds.add(binding['collection'])
            if len(kinds) != 1:
                return None
            kind = next(iter(kinds))
        rule = 'FLOW_BUILTIN_METHOD'
    if kind and method in METHODS.get(kind, set()):
        return kind, rule
    return None
