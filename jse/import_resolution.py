"""Static import provenance tracing. No project or imported code is executed.

Treat conditional imports as alternatives, not as the last lexical declaration.
Only direct source-backed symbols are eligible as call-graph targets.
"""
from __future__ import annotations

from collections import defaultdict
import builtins
import sys


def canonical_import(imp: dict) -> str:
    """Absolute name to which a Python import binds the local name."""
    level = imp.get('level') or 0
    if not level:
        return imp.get('imported_canonical') or ''
    current = imp.get('module') or ''
    package = current if imp.get('file', '').endswith('/__init__.py') else current.rpartition('.')[0]
    parts = package.split('.') if package else []
    # A single leading dot means the current package; two means its parent.
    if level > len(parts):
        return ''  # invalid/unknown relative import; don't fabricate target
    base = parts[:len(parts) - level + 1]
    return '.'.join(base + [x for x in (imp.get('imported_module'), imp.get('imported_name')) if x])


class ImportOrigins:
    """Resolve an imported name through module-level imports and simple aliases.

    Unlike a runtime importer this cannot prove attributes added dynamically or
    conditional import feasibility. Such cases remain unknown or alternatives.
    """

    def __init__(self, data: dict):
        self.modules = set(data.get('source_modules') or [])
        self.modules.update(s.get('module') for s in data.get('symbols', []))
        self.modules.update(c.get('module') for c in data.get('calls', []))
        self.modules.discard(None)
        self.defs = defaultdict(list)
        for s in data.get('symbols', []):
            self.defs[s['canonical_name']].append(s)
        self.exports = defaultdict(list)
        for imp in data.get('imports', []):
            if imp.get('scope') == '<module>' and imp.get('local_name'):
                self.exports[(imp['module'], imp['local_name'])].append(imp)
        self.assigns = defaultdict(list)
        for a in data.get('assignments', []):
            if a.get('scope') == '<module>':
                for target in a.get('targets', []):
                    if target.isidentifier():
                        self.assigns[(a['module'], target)].append(a)

    def _prefix(self, canonical: str):
        # Longest existing module prefix; supports e.g. requests.compat.urlparse.
        choices = [m for m in self.modules if canonical.startswith(m + '.')]
        if not choices:
            return None, None
        mod = max(choices, key=len)
        return mod, canonical[len(mod)+1:]

    @staticmethod
    def _live_choices(entries: list[dict]) -> list[dict]:
        """Last unconditional binding dominates earlier imports/assignments.

        Conditional bindings may coexist; model each as an alternative.
        """
        if not entries:
            return []
        entries = sorted(entries, key=lambda x: (x.get('line') or 0, x.get('column') or 0))
        last_def = max((i for i, x in enumerate(entries) if not x.get('control_depth')), default=-1)
        return entries[last_def:] if last_def >= 0 else entries

    def trace(self, canonical: str, _seen: frozenset | None = None) -> list[dict]:
        seen = _seen or frozenset()
        if not canonical or canonical in seen or len(seen) >= 16:
            return [{'kind': 'unknown', 'name': canonical, 'rule': 'cyclic_or_invalid_alias'}]
        seen = seen | {canonical}
        if canonical in self.modules:
            return [{'kind': 'internal_module', 'name': canonical, 'rule': 'source_module'}]
        mod, tail = self._prefix(canonical)
        if mod:
            head, dot, rest = tail.partition('.')
            # Compare the definitions, imports and simple assignments that
            # bind the SAME module-level name. Later unconditional bindings
            # shadow previous ones; conditional bindings remain alternatives.
            options = self.exports.get((mod, head), []) + self.assigns.get((mod, head), [])
            definitions = []
            if canonical in self.defs and not rest:
                definitions = [{'binding_kind': 'definition', 'line': s.get('line'),
                                'column': s.get('column'), 'control_depth': s.get('control_depth', 0),
                                'symbol_id': s['id']} for s in self.defs[canonical]]
            if options or definitions:
                results = []
                for item in self._live_choices(options + definitions):
                    if item.get('binding_kind') == 'definition':
                        results.append({'kind': 'internal', 'name': canonical,
                                        'rule': 'source_definition', 'symbol_ids': [item['symbol_id']]})
                        continue
                    if 'imported_canonical' in item:
                        target = canonical_import(item)
                    else:
                        binding = item.get('binding') or {}
                        # Only simple name/attribute aliases can be traced.
                        target = binding.get('name') if binding.get('kind') in ('name', 'attribute') else None
                        if target and target.split('.')[0] not in vars(builtins):
                            target = mod + '.' + target
                        if target in vars(builtins):
                            results.append({'kind': 'builtin', 'name': target, 'rule': 'builtin_alias'})
                            continue
                    if target:
                        target = target + ('.' + rest if rest else '')
                        for resolution in self.trace(target, seen):
                            results.append({**resolution,
                                            'via': [canonical, *(resolution.get('via') or [])]})
                    else:
                        results.append({'kind': 'unknown', 'name': canonical,
                                        'rule': 'dynamic_reexport_assignment'})
                return self._unique(results)
            # Nested definitions such as Class.method or local functions have
            # canonical AST symbols but aren't module-level re-exports.
            if canonical in self.defs:
                return [{'kind': 'internal', 'name': canonical, 'rule': 'source_definition',
                         'symbol_ids': [s['id'] for s in self.defs[canonical]]}]
            return [{'kind': 'unknown', 'name': canonical, 'rule': 'project_attribute_not_defined'}]
        if canonical in self.defs:
            return [{'kind': 'internal', 'name': canonical, 'rule': 'source_definition',
                     'symbol_ids': [s['id'] for s in self.defs[canonical]]}]
        root = canonical.split('.')[0]
        if root in sys.stdlib_module_names or root in sys.builtin_module_names or root == 'builtins':
            return [{'kind': 'stdlib', 'name': canonical, 'rule': 'stdlib_origin'}]
        if root in {m.split('.')[0] for m in self.modules}:
            return [{'kind': 'unknown', 'name': canonical, 'rule': 'project_module_not_found'}]
        return [{'kind': 'external', 'name': canonical, 'rule': 'external_origin'}]

    @staticmethod
    def _unique(values: list[dict]) -> list[dict]:
        seen, result = set(), []
        for value in values:
            key = (value.get('kind'), value.get('name'), tuple(value.get('symbol_ids') or []))
            if key not in seen:
                seen.add(key)
                result.append(value)
        return result

    def project_symbols(self, canonical: str) -> list[dict]:
        origins = self.trace(canonical)
        # Never emit a definite call edge if any alternative is external,
        # builtin, unknown or only a module instead of a callable definition.
        if not origins or any(x['kind'] != 'internal' for x in origins):
            return []
        ids = {sid for x in origins for sid in x.get('symbol_ids', [])}
        return [s for group in self.defs.values() for s in group if s['id'] in ids]
