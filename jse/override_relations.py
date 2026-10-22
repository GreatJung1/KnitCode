"""Source declaration relations, independent of call sites and dispatch.

Every source-backed ancestor definition is related to a direct child definition.
The C3 index records precedence, not a claim that every ancestor is dispatched.
"""
import ast


def overridden_methods(graph: dict, child_method_id: str) -> list[dict]:
    """Reverse lookup: return incoming OVERRIDDEN_BY edges, preserving evidence."""
    return [edge for edge in graph.get('edges', [])
            if edge['type'] == 'OVERRIDDEN_BY' and edge['target'] == child_method_id]


def _class_declarations(resolver):
    # Symbols omit class control_depth. Consult the already captured source;
    # never read/execute an external library or recollect a repository.
    declarations = {}
    class_files = {cls['file'] for cls in resolver.classes.values()}
    for file, source in (resolver.data.get('analysis_sources') or {}).items():
        if file not in class_files:
            continue
        try:
            tree = ast.parse(source)
        except (SyntaxError, ValueError):
            continue
        parents = {child: node for node in ast.walk(tree) for child in ast.iter_child_nodes(node)}
        for node in ast.walk(tree):
            if not isinstance(node, ast.ClassDef):
                continue
            ancestor = parents.get(node)
            unconditional = True
            while ancestor is not None:
                if isinstance(ancestor, ast.stmt) and not isinstance(
                        ancestor, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
                    unconditional = False
                ancestor = parents.get(ancestor)
            if unconditional:
                declarations[file, node.lineno] = node
    return declarations


def _decorator_is_builtin(resolver, cls, name):
    # A same-spelled custom decorator must not be certified as a descriptor.
    scopes = {'<module>', cls['qualified_name']}
    parts = (cls.get('scope') or '').split('.')
    scopes.update('.'.join(parts[:i]) for i in range(1, len(parts) + 1))
    return not (
        any(s['file'] == cls['file'] and s.get('scope') in scopes and s['name'] == name
            for s in resolver.symbols)
        or any(name in a.get('targets', []) for scope in scopes
               for a in resolver.assignments.get((cls['file'], scope), []))
        or any(i.get('scope') in scopes and i.get('local_name') in (name, '*')
               for i in resolver.imports[cls['file']])
        or any(h['file'] == cls['file'] and h.get('scope') in scopes and h.get('name') == name
               for h in resolver.data.get('binding_hazards', [])))


def _members(resolver, cls, declaration):
    result = {}
    for node in declaration.body:
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        name = node.name
        # Private names are mangled with the declaring class, so equal source
        # spelling in different classes is not an override.
        if name.startswith('__') and not name.endswith('__'):
            continue
        candidates = resolver._lookup(cls['canonical_name'] + '.' + name)
        if len(candidates) != 1:
            continue
        method = candidates[0]
        if (method['kind'] != 'METHOD' or method.get('control_depth')
                or method.get('alternate_definition_lines') or method['line'] != node.lineno):
            continue
        scope = cls['qualified_name']
        if (any(name in a.get('targets', []) for a in resolver.assignments.get((cls['file'], scope), []))
                or any(i.get('scope') == scope and i.get('local_name') in (name, '*')
                       for i in resolver.imports[cls['file']])
                or any(h['file'] == cls['file'] and h.get('scope') == scope and h.get('name') == name
                       for h in resolver.data.get('binding_hazards', []))):
            continue
        decorators = method.get('decorators', [])
        kind = 'instance_method'
        if decorators:
            if len(decorators) != 1 or decorators[0] not in ('staticmethod', 'classmethod', 'property'):
                continue
            kind = decorators[0]
            if not _decorator_is_builtin(resolver, cls, kind):
                continue
        # Reuse ordinary-method eligibility. Properties are declaration-only;
        # they intentionally remain excluded from the CALLS resolver.
        if kind != 'property' and method not in resolver._local_method(cls, name)[0]:
            continue
        result[name] = (method, kind)
    return result


def build_override_edges(resolver) -> list[dict]:
    declarations = _class_declarations(resolver)
    classes = {name: cls for name, cls in resolver.classes.items()
               if len(resolver._lookup(name)) == 1
               and (cls['file'], cls['line']) in declarations}
    members = {name: _members(resolver, cls, declarations[cls['file'], cls['line']])
               for name, cls in classes.items()}
    edges = []
    for name, cls in classes.items():
        order, status = resolver._mro(cls)
        if status != 'complete' or any(key in resolver.classes and key not in classes for key in order):
            continue
        for method_name, (child, child_kind) in members[name].items():
            for index, ancestor in enumerate(order[1:], 1):
                parent_entry = members.get(ancestor, {}).get(method_name)
                if parent_entry is None:
                    continue
                parent, parent_kind = parent_entry
                def location(symbol, kind):
                    return {**{k: symbol.get(k) for k in
                               ('id', 'canonical_name', 'file', 'line', 'column', 'end_line', 'end_column')},
                            'member_kind': kind, 'async': symbol.get('async', False)}
                edges.append({
                    'source': parent['id'], 'target': child['id'], 'type': 'OVERRIDDEN_BY',
                    'evidence': {
                        'file': child['file'], 'line': child['line'],
                        'rule': 'source_defined_ancestor_member_c3',
                        'relation_scope': 'all_defining_ancestors',
                        'mro': order, 'ancestor_mro_index': index,
                        'parent': location(parent, parent_kind), 'child': location(child, child_kind),
                        'signature_checked': False,
                    },
                })
    return edges
