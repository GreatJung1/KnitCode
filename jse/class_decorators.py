"""Narrow source proof for local methods on decorated, otherwise plain classes.

This does not certify an MRO or create structural override relations. The source
subset admits identity returns and repr-only mutations, including local helper
functions. Arbitrary calls, class replacement and hierarchy mutations fail closed.
"""
import ast


def preserves_local_method(resolver, cls, method_name):
    if (not cls.get('decorators') or cls.get('class_keywords')
            or cls.get('bases') or method_name.startswith('__')):
        return False
    context = {**cls, 'scope': cls.get('scope') or '<module>'}
    imports = resolver.imports_at(context)

    def prove(symbol, active=frozenset()):
        if (symbol['id'] in active or symbol.get('decorators')
                or symbol.get('control_depth') or symbol.get('alternate_definition_lines')):
            return False
        source = resolver.data.get('analysis_sources', {}).get(symbol['file'])
        if not source:
            return False
        tree = ast.parse(source)
        nodes = [n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)
                 and n.lineno == symbol['line']]
        if len(nodes) != 1:
            return False
        module_imports = resolver.imports_at({**symbol, 'scope': symbol['qualified_name']})

        def function(fn, local, seen):
            if fn in seen or not fn.args.args:
                return False
            receiver = fn.args.args[0].arg
            local = {**local, **{n.name: n for n in fn.body if isinstance(n, ast.FunctionDef)}}

            def identity(expr):
                if isinstance(expr, ast.Name):
                    return expr.id == receiver
                if (not isinstance(expr, ast.Call) or len(expr.args) != 1
                        or not isinstance(expr.args[0], ast.Name) or expr.args[0].id != receiver
                        or not isinstance(expr.func, ast.Name)
                        or any(not isinstance(k.value, (ast.Name, ast.Constant)) for k in expr.keywords)):
                    return False
                if expr.func.id in local:
                    return function(local[expr.func.id], local, seen | {fn})
                if expr.func.id in {p.arg for p in [*fn.args.args, *fn.args.kwonlyargs]}:
                    return False
                # Deferred helper calls observe module bindings, including
                # assignments after this function's definition.
                if (any(expr.func.id in a.get('targets', [])
                        for a in resolver.assignments.get((symbol['file'], '<module>'), []))
                        or any(i.get('local_name') == '*' for i in resolver.imports[symbol['file']])):
                    return False
                targets = resolver._resolve_reference(expr.func.id, symbol['module'], module_imports)
                return len(targets) == 1 and prove(targets[0], active | {symbol['id']})

            def block(body):
                returned = False
                for stmt in body:
                    if returned:
                        break
                    if isinstance(stmt, ast.FunctionDef):
                        # Definitions themselves must not execute user code.
                        annotations = [p.annotation for p in
                                       [*stmt.args.posonlyargs, *stmt.args.args, *stmt.args.kwonlyargs]]
                        annotations.append(stmt.returns)
                        if stmt.decorator_list or any(not isinstance(x, ast.Constant)
                                for x in [*stmt.args.defaults, *[x for x in stmt.args.kw_defaults if x]]) or any(
                                isinstance(n, ast.Call) for annotation in annotations if annotation
                                for n in ast.walk(annotation)):
                            return False, False
                    elif isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Constant):
                        pass
                    elif isinstance(stmt, ast.Assign):
                        if not isinstance(stmt.value, ast.Name) or stmt.value.id not in local:
                            # Rich assigns docstrings and the angular flag too.
                            if not isinstance(stmt.value, (ast.Constant, ast.Name)):
                                return False, False
                        for target in stmt.targets:
                            path = ast.unparse(target).split('.')
                            allowed = (path[0] == receiver and path[1:] in
                                       (['__repr__'], ['__rich_repr__'], ['__rich_repr__', 'angular']))
                            allowed |= path[0] in local and path[1:] == ['__doc__']
                            if not allowed:
                                return False, False
                    elif isinstance(stmt, ast.Return):
                        return identity(stmt.value), True
                    elif isinstance(stmt, ast.If):
                        test = stmt.test
                        # The actual argument is a class, never None.
                        if (isinstance(test, ast.Compare) and isinstance(test.left, ast.Name)
                                and test.left.id == receiver and len(test.ops) == 1
                                and isinstance(test.ops[0], ast.Is)
                                and isinstance(test.comparators[0], ast.Constant)
                                and test.comparators[0].value is None):
                            ok, returned = block(stmt.orelse)
                        else:
                            # Only inert None comparisons / builtin hasattr.
                            safe_test = (isinstance(test, ast.Compare)
                                         and all(isinstance(op, (ast.Is, ast.IsNot)) for op in test.ops)
                                         and all(isinstance(n, (ast.Name, ast.Constant))
                                                 for n in [test.left, *test.comparators]))
                            call = test.operand if isinstance(test, ast.UnaryOp) and isinstance(test.op, ast.Not) else test
                            if isinstance(call, ast.Call) and isinstance(call.func, ast.Name) and call.func.id == 'hasattr':
                                safe_test = (len(call.args) == 2 and isinstance(call.args[0], ast.Name)
                                             and call.args[0].id == receiver and isinstance(call.args[1], ast.Constant)
                                             and call.args[1].value == '__rich_repr__'
                                             and 'hasattr' not in {p.arg for p in [*fn.args.args, *fn.args.kwonlyargs]}
                                             and 'hasattr' not in module_imports
                                             and not resolver._lookup(symbol['module'] + '.hasattr')
                                             and not resolver._find_binding(symbol, 'hasattr'))
                            if not safe_test:
                                return False, False
                            left, lr = block(stmt.body); right, rr = block(stmt.orelse)
                            ok, returned = left and right, lr and rr
                        if not ok:
                            return False, False
                    else:
                        return False, False
                return True, returned

            ok, returned = block(fn.body)
            return ok and returned

        return function(nodes[0], {}, set())

    for text in cls['decorators']:
        expr = ast.parse(text, mode='eval').body
        if not isinstance(expr, (ast.Name, ast.Attribute)):
            return False
        root = text.split('.')[0]
        if resolver._find_binding(context, root) or resolver._find_binding(context, text):
            return False
        origin = imports.get(root)
        if origin and not resolver._trusted_stdlib_import(cls, root, origin):
            return False
        canonical = (origin + text[len(root):]) if origin else cls['module'] + '.' + text
        if canonical == 'dataclasses.dataclass' and 'dataclasses' not in resolver.origins.modules:
            continue  # Ordinary methods are preserved; generated dunders excluded.
        targets = resolver._resolve_reference(text, cls['module'], imports)
        if len(targets) != 1 or not prove(targets[0]):
            return False
    return True
