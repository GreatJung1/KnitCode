"""Optional bounded, source-based stdlib/third-party static-call inspection.

Never import, execute, install or download analyzed modules. Imported package
source (.py, then .pyi API-only fallback) is indexed from explicit filesystem
roots. Cross-boundary targets and selected dependency-internal calls are
reported in dependency_graph.json, separate from the project's existing graph.
"""
from __future__ import annotations

import ast
import csv
from dataclasses import dataclass
import json
from pathlib import Path
import sys
import sysconfig

from call_status import StatusAnnotator
from import_resolution import canonical_import


@dataclass
class SourceDef:
    canonical: str
    kind: str
    module: str
    path: str
    line: int
    source_kind: str
    availability: str  # PY_SOURCE or API_STUB
    bases: tuple[str, ...] = ()

    def to_dict(self):
        return {'id': 'DEP:' + self.kind + ':' + self.canonical,
                'canonical_name': self.canonical, 'kind': self.kind,
                'module': self.module, 'file': self.path, 'line': self.line,
                'origin_kind': self.source_kind,
                'availability': self.availability, 'bases': list(self.bases)}


class DependencyIndex:
    def __init__(self, project_symbols, *, roots=(), include_stdlib=True, include_installed=True,
                 max_files=300):
        self.roots: list[tuple[Path, str]] = []
        self.symbols = {}
        self.aliases = {}
        self.star_imports = {}
        self.exports = {}
        self.modules = set()
        self.trees = {}
        self.file_count = 0
        self.max_files = max_files
        self.errors = []
        self.skipped = []
        self.project_roots = {m.split('.')[0] for m in project_symbols.get('source_modules', []) if m}
        self.loaded_roots = set()
        self.source_roots = {}
        self.checked_modules = set()
        self.consumed = set()
        # Do not walk sys.path: it may contain untrusted or arbitrary locations.
        if include_stdlib:
            self._add_root(sysconfig.get_path('stdlib'), 'STDLIB')
        # Explicit target-venv sources precede the analyzer's own environment:
        # library versions matter when mapping definitions to source lines.
        for p in roots:
            self._add_root(p, 'EXTERNAL_IMPORT')
        if include_installed:
            for key in ('purelib', 'platlib'):
                self._add_root(sysconfig.get_path(key), 'EXTERNAL_IMPORT')

    def _add_root(self, path, kind):
        if path and Path(path).is_dir() and (Path(path).resolve(), kind) not in self.roots:
            self.roots.append((Path(path).resolve(), kind))

    @staticmethod
    def _module_name(p: Path, package_dir: Path, top: str):
        relative = p.relative_to(package_dir).with_suffix('')
        names = [] if relative.name == '__init__' and len(relative.parts) == 1 else list(relative.parts)
        if names and names[-1] == '__init__': names.pop()
        return '.'.join([top, *names])

    def _source_locations(self, top):
        for root, kind in self.roots:
            # Accept site-packages/, a package directory, or a stdlib root.
            if root.name == top and ((root / '__init__.py').exists() or
                                     (root / '__init__.pyi').exists()):
                yield root, kind, 'package'
            pkg = root / top
            if (pkg / '__init__.py').is_file() or (pkg / '__init__.pyi').is_file():
                yield pkg, kind, 'package'
            elif pkg.is_dir():
                yield pkg, kind, 'namespace'
            elif (root / (top + '.py')).is_file():
                yield root / (top + '.py'), kind, 'file'
            elif (root / (top + '.pyi')).is_file():
                yield root / (top + '.pyi'), kind, 'file'

    def _read_module(self, path, module, kind):
        if path in self.consumed or not path.is_file(): return
        if self.file_count >= self.max_files:
            if not any(x.get('root') == module and x['reason'] == 'file_limit' for x in self.skipped):
                self.skipped.append({'root': module, 'reason': 'file_limit', 'remaining_unindexed': True})
            return
        self.consumed.add(path)
        self._parse(path, module, kind)

    @staticmethod
    def _source_file(base: Path):
        for p in (base.with_suffix('.py'), base.with_suffix('.pyi'),
                  base / '__init__.py', base / '__init__.pyi'):
            if p.is_file(): return p
        return None

    def load(self, top):
        if top in self.loaded_roots or top in self.project_roots:
            return
        self.loaded_roots.add(top)
        for src, kind, shape in self._source_locations(top):
            self.source_roots[top] = (src, kind, shape)
            if shape == 'file':
                self._read_module(src, top, kind)
            else:
                entry = self._source_file(src / '__init__')
                # The namespace package might not have __init__.
                if entry:
                    self._read_module(entry, top, kind)
            return
        self.skipped.append({'root': top, 'reason': 'source_not_found'})

    def ensure_module(self, canonical):
        """Index only modules reachable from actually requested symbol names."""
        parts = canonical.split('.')
        if not parts or parts[0] in self.project_roots:
            return
        top = parts[0]
        self.load(top)
        source_root = self.source_roots.get(top)
        if not source_root:
            return
        src, kind, shape = source_root
        if shape == 'file': return
        for n in range(2, len(parts) + 1):
            module = '.'.join(parts[:n])
            if module in self.checked_modules or module in self.modules: continue
            self.checked_modules.add(module)
            p = self._source_file(src.joinpath(*parts[1:n]))
            if p: self._read_module(p, module, kind)

    @staticmethod
    def _import_name(node, module, init_module):
        if isinstance(node, ast.ImportFrom):
            parent = module if init_module else module.rpartition('.')[0]
            prefix = parent.split('.') if parent else []
            if node.level:
                if node.level > len(prefix): return None
                prefix = prefix[:len(prefix) - node.level + 1]
                return '.'.join([*prefix, *([node.module] if node.module else [])])
            return node.module
        return None

    def _parse(self, p, module, origin):
        try:
            if p.stat().st_size > 2_000_000:
                self.skipped.append({'root': module, 'reason': 'source_file_too_large'})
                return
            source = p.read_text(encoding='utf-8-sig')
            tree = ast.parse(source, filename=str(p))
        except (SyntaxError, UnicodeError, OSError) as e:
            self.errors.append({'file': str(p), 'error': str(e)[:180]})
            return
        self.file_count += 1
        self.modules.add(module)
        self.trees[module] = tree
        init_module = p.stem == '__init__'
        for item in tree.body:
            if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                kind = 'CLASS' if isinstance(item, ast.ClassDef) else 'FUNCTION'
                bases = tuple(ast.unparse(b) for b in item.bases) if kind == 'CLASS' else ()
                cname = module + '.' + item.name
                self.symbols[cname] = SourceDef(cname, kind, module, str(p), item.lineno, origin,
                                                 'API_STUB' if p.suffix == '.pyi' else 'PY_SOURCE', bases)
                if kind == 'CLASS':
                    for member in item.body:
                        if isinstance(member, (ast.FunctionDef, ast.AsyncFunctionDef)):
                            name = cname + '.' + member.name
                            self.symbols[name] = SourceDef(name, 'METHOD', module, str(p), member.lineno, origin,
                                                            'API_STUB' if p.suffix == '.pyi' else 'PY_SOURCE')
            elif isinstance(item, ast.ImportFrom):
                target = self._import_name(item, module, init_module)
                if target:
                    for alias in item.names:
                        if alias.name == '*':
                            self.star_imports.setdefault(module, []).append(target)
                        else:
                            self.aliases[module + '.' + (alias.asname or alias.name)] = target + '.' + alias.name
            elif isinstance(item, ast.Import):
                for alias in item.names:
                    self.aliases[module + '.' + (alias.asname or alias.name.split('.')[0])] = alias.name
            elif (isinstance(item, ast.Assign) and any(isinstance(t, ast.Name) and t.id == '__all__'
                                                       for t in item.targets)):
                if isinstance(item.value, (ast.List, ast.Tuple, ast.Set)) and all(
                        isinstance(x, ast.Constant) and isinstance(x.value, str) for x in item.value.elts):
                    self.exports[module] = {x.value for x in item.value.elts}

    def resolve(self, canonical, seen=None):
        """Follow AST-import reexports only, never arbitrary name matching."""
        seen = set() if seen is None else seen
        if not canonical or canonical in seen or len(seen) > 15:
            return None
        seen.add(canonical)
        self.ensure_module(canonical)
        found = self.symbols.get(canonical)
        if found: return found
        # Strip only real alias prefixes; do not infer a module from matching suffix.
        for prefix in sorted(self.aliases, key=len, reverse=True):
            if canonical == prefix or canonical.startswith(prefix + '.'):
                tail = canonical[len(prefix):]
                return self.resolve(self.aliases[prefix] + tail, seen)
        if '.' in canonical:
            mod, leaf = canonical.rsplit('.', 1)
            # Star reexports: only if target has a literal __all__ naming the
            # symbol. Ambiguous origins remain unlinked.
            matches = []
            for source_mod in self.star_imports.get(mod, []):
                if leaf in self.exports.get(source_mod, set()):
                    target = self.resolve(source_mod + '.' + leaf, seen.copy())
                    if target: matches.append(target)
            if len({m.canonical for m in matches}) == 1:
                return matches[0]
        return None

    def _resolve_inherited(self, canonical, seen=None):
        seen = set() if seen is None else seen
        if canonical in seen or len(seen) > 16: return None
        seen.add(canonical)
        found = self.resolve(canonical)
        if found or '.' not in canonical: return found
        owner, method = canonical.rsplit('.', 1)
        cls = self.resolve(owner)
        if not cls or cls.kind != 'CLASS': return None
        # Search the first base ONLY: later bases are not provable without
        # full C3 MRO/override analysis. Resolve explicit import aliases.
        if not cls.bases: return None
        base = cls.bases[0]
        if '.' not in base: base = cls.module + '.' + base
        base_def = self.resolve(base)
        if not base_def or base_def.kind != 'CLASS': return None
        return self._resolve_inherited(base_def.canonical + '.' + method, seen)

    def nodes_and_edges(self, *, max_edges=50000):
        """Inspect limited, syntactically direct dependency-internal calls."""
        edges = []
        # Lookups may index additional import targets. Traverse a stable
        # snapshot, leaving newly discovered modules for the next iteration.
        processed = set()
        while len(processed) < len(self.trees):
            pending = [(m, t) for m, t in self.trees.items() if m not in processed]
            if not pending: break
            module, tree = pending[0]
            processed.add(module)
            for item in tree.body:
                if not isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                    continue
                fs = ([(item, module + '.' + item.name)] if not isinstance(item, ast.ClassDef) else
                      [(m, module + '.' + item.name + '.' + m.name) for m in item.body
                       if isinstance(m, (ast.FunctionDef, ast.AsyncFunctionDef))])
                for fn, caller in fs:
                    for node in ast.walk(fn):
                        if not isinstance(node, ast.Call): continue
                        if isinstance(node.func, ast.Name):
                            name = module + '.' + node.func.id
                        elif (isinstance(node.func, ast.Attribute) and isinstance(node.func.value, ast.Name)):
                            parent = node.func.value.id
                            name = ((module + '.' + item.name + '.' + node.func.attr)
                                    if parent in ('self','cls') and isinstance(item, ast.ClassDef)
                                    else module + '.' + parent + '.' + node.func.attr)
                        else:
                            continue
                        target = self.resolve(name)
                        if not target or target.availability != 'PY_SOURCE': continue
                        edges.append({'source': self.symbols[caller].to_dict()['id'],
                                      'target': target.to_dict()['id'], 'type': 'CALLS',
                                      'source_line': node.lineno, 'rule': 'DEPENDENCY_AST_SYNTAX',
                                      'certainty': 'MAY_CALL'})
                        if len(edges) >= max_edges: return edges
        return edges


def analyze_dependencies(symbols, graph, *, roots=(), include_stdlib=True,
                         include_installed=True, max_files=300):
    index = DependencyIndex(symbols, roots=roots, include_stdlib=include_stdlib,
                            include_installed=include_installed, max_files=max_files)
    imports = symbols.get('imports', [])
    for imp in imports:
        canonical = canonical_import(imp)
        if canonical:
            index.load(canonical.split('.')[0])
    # Static os.path alias is platform dependent (posixpath or ntpath).
    if 'os' in index.loaded_roots:
        index.load('posixpath')
        index.load('ntpath')
    status = StatusAnnotator(symbols)
    by_id = {row['call_id']: row for row in graph.get('call_statuses', [])}
    inspected = []
    project_edges = []
    origins = []
    for call in symbols.get('calls', []):
        row = by_id.get(call['id'], {})
        if row.get('status') == 'RESOLVED_INTERNAL': continue
        callee = call.get('callee_text') or ''
        head, dot, rest = callee.partition('.')
        if not head.isidentifier() and '()' not in head: continue
        choices = []
        provenance = []
        # Direct imported symbol or module attribute, provided import was not
        # shadowed or conditional. A source definition is not runtime proof.
        imp, conflicts, _ = status._bound_at(call, head) if head.isidentifier() else (None, [], False)
        if imp and not conflicts:
            # Assignment to a module attribute may replace its imported
            # callable (e.g. json.dumps = custom); don't report it as proved.
            path_parts = rest.split('.') if rest else []
            dotted = [head + '.' + '.'.join(path_parts[:n])
                      for n in range(1, len(path_parts) + 1)]
            overwritten = any(file == call['file'] and target in dotted
                              for file, _scope, target in status.class_assignments)
            canonical = canonical_import(imp)
            if not overwritten and canonical and canonical.split('.')[0] not in index.project_roots:
                choices.append(canonical + ('.' + rest if dot else ''))
                provenance.append('unshadowed_import')
        # Instance methods: track constructor objects from flow, but only
        # permit names with proven imported constructors (not random methods).
        if dot and rest.isidentifier() and head.isidentifier():
            # An instance-level overwrite invalidates source-method evidence.
            override = any(file == call['file'] and target == head + '.' + rest
                           for file, _scope, target in status.class_assignments)
            flow = call.get('flow') or {}
            b = flow.get('binding') or {}
            if not override and flow.get('variable') == head and not b.get('may_be_unbound'):
                opts = b.get('options') or []
                if opts and all(x.get('kind') == 'constructed' for x in opts):
                    for opt in opts:
                        typename = opt.get('name') or ''
                        constructor_head, separator, tail = typename.partition('.')
                        imported, bad, _ = status._bound_at(call, constructor_head)
                        if imported and not bad:
                            root = canonical_import(imported)
                            if root and root.split('.')[0] not in index.project_roots:
                                choices.append(root + ('.' + tail if separator else '') + '.' + rest)
                                provenance.append('flow_constructor')
        # A call performed on an imported class expression (e.g. X().run()).
        if not choices and '()' in callee and (call.get('source') or ''):
            try: expr = ast.parse(call['source'], mode='eval').body
            except (SyntaxError, ValueError): expr = None
            if (isinstance(expr, ast.Call) and isinstance(expr.func, ast.Attribute)
                    and isinstance(expr.func.value, ast.Call)):
                ctor = expr.func.value.func
                if isinstance(ctor, ast.Name):
                    imported, bad, _ = status._bound_at(call, ctor.id)
                    if imported and not bad:
                        choices.append(canonical_import(imported) + '.' + expr.func.attr)
                        provenance.append('inline_constructor')
        # A source location for os.path.join is necessarily platform-specific.
        expanded = []
        for choice in choices:
            if choice.startswith('os.path.'):
                expanded.extend([('posixpath.' + choice[len('os.path.'):], 'PLATFORM_DEPENDENT'),
                                 ('ntpath.' + choice[len('os.path.'):], 'PLATFORM_DEPENDENT')])
            else:
                expanded.append((choice, 'SOURCE_CANDIDATE'))
        matches = {}
        for choice, certainty in expanded:
            found = index._resolve_inherited(choice)
            if found:
                matches[found.to_dict()['id']] = (found, certainty)
        if not choices: continue
        if matches:
            for target, certainty in matches.values():
                if target.kind not in ('CLASS', 'FUNCTION', 'METHOD'): continue
                project_edges.append({'source': call.get('caller_id') or 'MODULE:' + call['module'],
                                      'target': target.to_dict()['id'], 'type': 'INSTANTIATES' if target.kind == 'CLASS' else 'CALLS',
                                      'evidence': {'call_id': call['id'], 'file': call['file'], 'line': call['line'],
                                                   'rule': 'IMPORTED_SOURCE_DEFINITION',
                                                   'certainty': 'API_ONLY' if target.availability == 'API_STUB' else
                                                                'MAY_CALL_' + certainty, 'provenance': provenance}})
            if any(t.availability == 'PY_SOURCE' for t, _ in matches.values()):
                result = 'SOURCE_FOUND'
            else:
                result = 'API_STUB_ONLY'
        else:
            result = 'ORIGIN_ONLY_NO_DEFINITION'
        inspected.append({'call_id': call['id'], 'file': call['file'], 'line': call['line'],
                          'callee_text': callee, 'result': result,
                          'candidate_paths': [x for x, _ in expanded],
                          'target_ids': sorted(matches), 'provenance': sorted(set(provenance))})
    # Index contains only dependency files from selected top-level imports;
    # local project graph/node/status partition stays identical to v2.4.
    library_edges = index.nodes_and_edges()
    result = {
        'project_repository': symbols.get('repository'),
        'policy': 'STATIC_SOURCE_ONLY; imported sources never executed; may-call graph, not runtime truth',
        'summary': {'indexed_dependency_python_files': index.file_count,
                    'source_definitions': len(index.symbols),
                    'project_calls_with_dependency_origin': len(inspected),
                    'project_calls_with_source_definition': sum(c['result'] == 'SOURCE_FOUND' for c in inspected),
                    'project_calls_with_stub_definition': sum(c['result'] == 'API_STUB_ONLY' for c in inspected),
                    'project_calls_origin_only': sum(c['result'] == 'ORIGIN_ONLY_NO_DEFINITION' for c in inspected),
                    'project_to_dependency_edges': len(project_edges),
                    'dependency_internal_edges': len(library_edges),
                    'file_limit': max_files, 'truncated': bool(any(x['reason'] == 'file_limit' for x in index.skipped))},
        'indexed_roots': sorted(index.loaded_roots),
        'unindexed': index.skipped, 'parse_errors': index.errors,
        'nodes': [x.to_dict() for x in index.symbols.values()],
        'project_edges': project_edges, 'dependency_edges': library_edges,
        'project_calls': inspected,
    }
    return result


def write_dependency_results(analysis, out):
    out = Path(out);out.mkdir(parents=True, exist_ok=True)
    (out / 'dependency_graph.json').write_text(json.dumps(analysis, ensure_ascii=False, indent=2), encoding='utf-8')
    with (out / 'dependency_calls.csv').open('w', encoding='utf-8-sig', newline='') as f:
        fields = ['file', 'line', 'callee_text', 'result', 'candidate_paths', 'target_ids', 'provenance']
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        for entry in analysis['project_calls']:
            w.writerow({k: ', '.join(entry[k]) if isinstance(entry[k], list) else entry[k] for k in fields})
