from __future__ import annotations

import json
from pathlib import Path
from typing import Any


def load_json(path: str | Path) -> dict[str, Any]:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def save_json(data: dict[str, Any], path: str | Path) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def normalize_path(path: str) -> str:
    return path.replace("\\", "/").lstrip("./")


def module_name_from_file(file_path: str, repository_name: str | None,
                          source_roots: tuple[str, ...] | list[str] = ()) -> str:
    """
    sample_repo\\payment.py -> payment
    sample_repo\\tests\\test_payment.py -> tests.test_payment
    """
    normalized = normalize_path(file_path)

    if repository_name:
        repo = normalize_path(repository_name).rstrip("/")
        prefix = repo + "/"
        if normalized.startswith(prefix):
            normalized = normalized[len(prefix):]
        elif f"/{prefix}" in normalized:
            normalized = normalized.split(f"/{prefix}", 1)[1]

    # File evidence retains its full repository-relative path; only canonical
    # import identities discard a declared source-root directory.
    for source_root in sorted(source_roots, key=len, reverse=True):
        prefix = normalize_path(source_root).rstrip('/') + '/'
        if normalized.startswith(prefix):
            normalized = normalized[len(prefix):]
            break

    if normalized.endswith("/__init__.py"):
        normalized = normalized[:-len("/__init__.py")]
    elif normalized == "__init__.py":
        normalized = ""
    elif normalized.endswith(".py"):
        normalized = normalized[:-3]

    return normalized.replace("/", ".")


def location(item: dict[str, Any]) -> dict[str, Any]:
    return {
        "line": item.get("line"),
        "column": item.get("column"),
        "end_line": item.get("end_line"),
        "end_column": item.get("end_column"),
    }


def span_contains(outer: dict[str, Any], inner: dict[str, Any]) -> bool:
    keys = ("line", "column", "end_line", "end_column")
    if any(outer.get(k) is None for k in keys):
        return False
    if any(inner.get(k) is None for k in keys):
        return False

    outer_start = (outer["line"], outer["column"])
    outer_end = (outer["end_line"], outer["end_column"])
    inner_start = (inner["line"], inner["column"])
    inner_end = (inner["end_line"], inner["end_column"])

    return outer_start <= inner_start and inner_end <= outer_end


def build_symbol_result(ast_result: dict[str, Any]) -> dict[str, Any]:
    repository = ast_result.get("repository")
    source_roots = ast_result.get("source_roots") or []

    symbols: list[dict[str, Any]] = []
    imports: list[dict[str, Any]] = []
    calls: list[dict[str, Any]] = []
    assignments: list[dict[str, Any]] = []
    binding_hazards: list[dict[str, Any]] = []

    # (file, qualified_name) -> symbol id
    local_index: dict[tuple[str, str], str] = {}

    # v2.3: @overload declarations describe typing signatures, not callable
    # implementations. Prefer the implementation and store the declarations as
    # metadata, never as duplicate graph nodes / edges.
    overload_declarations: list[dict[str, Any]] = []
    def is_overload(fn, aliases):
        return any(aliases.get((d or '').split('(')[0].strip().split('.')[0],
                               (d or '').split('(')[0].strip()) == 'typing.overload'
                   or ((d or '').split('(')[0].strip().endswith('.overload')
                       and aliases.get((d or '').split('(')[0].strip().rsplit('.', 1)[0]) == 'typing')
                   for d in fn.get('decorators', []))

    for file_info in ast_result.get('files', []):
        file_path = normalize_path(file_info.get('file', ''))
        module = module_name_from_file(file_path, repository, source_roots)
        for cls in file_info.get('classes', []):
            qname = cls.get('qualified_name') or cls.get('name')
            canonical = '.'.join(x for x in (module, qname) if x)
            symbol_id = f"CLASS:{canonical}"
            symbols.append({
                'id': symbol_id, 'kind': 'CLASS', 'name': cls.get('name'),
                'qualified_name': qname, 'canonical_name': canonical,
                'module': module, 'file': file_path, 'scope': cls.get('scope'),
                'bases': cls.get('bases', []), 'decorators': cls.get('decorators', []),
                'class_keywords': cls.get('class_keywords', []),
                **location(cls),
            })
            local_index[(file_path, qname)] = symbol_id

        decorator_aliases = {}
        for imp in file_info.get('imports', []):
            name = imp.get('name')
            module_path = imp.get('module')
            alias = imp.get('alias') or name or (module_path or '').split('.')[0]
            if module_path in ('typing', 'typing_extensions') and name == 'overload':
                decorator_aliases[alias] = 'typing.overload'
            elif module_path in ('typing', 'typing_extensions') and imp.get('type') == 'IMPORT':
                decorator_aliases[alias] = 'typing'
        groups = {}
        for fn in file_info.get('functions', []):
            groups.setdefault((fn.get('kind', 'FUNCTION'), fn.get('qualified_name') or fn.get('name')), []).append(fn)
        for (kind, qname), group in groups.items():
            declarations = [fn for fn in group if is_overload(fn, decorator_aliases)]
            concrete = [fn for fn in group if not is_overload(fn, decorator_aliases)]
            canonical = '.'.join(x for x in (module, qname) if x)
            if declarations:
                overload_declarations.append({
                    'canonical_name': canonical, 'kind': kind,
                    'file': file_path,
                    'lines': [fn.get('line') for fn in declarations],
                    'has_implementation': bool(concrete),
                    'signatures': [{'line': d.get('line'), 'parameters': d.get('parameters'),
                                    'return_annotation': d.get('return_annotation')}
                                   for d in declarations],
                })
            if not concrete:
                # An @overload-only set has no callable implementation.
                continue
            # Preserve distinct, unconditional module function bindings. Other
            # redefinitions (conditional, class, local) need control-flow- or
            # method-specific semantics; keep their existing conservative
            # representation instead of creating speculative target edges.
            preserve_versions = (kind == 'FUNCTION' and len(concrete) > 1 and
                                 all(fn.get('scope') == '<module>' and
                                     not fn.get('control_depth') and
                                     not fn.get('decorators') for fn in concrete))
            selected = concrete if preserve_versions else concrete[-1:]
            for version, fn in enumerate(selected):
                is_latest = version == len(selected) - 1
                base_id = f"{kind}:{canonical}"
                symbol_id = (base_id if is_latest else
                             f"{base_id}@L{fn.get('line')}C{fn.get('column')}")
                symbols.append({
                    'id': symbol_id, 'kind': kind, 'name': fn.get('name'),
                    'qualified_name': qname, 'canonical_name': canonical,
                    'module': module, 'file': file_path, 'scope': fn.get('scope'),
                    'parameters': fn.get('parameters', []),
                    'parameter_annotations': fn.get('parameter_annotations', {}),
                    'return_annotation': fn.get('return_annotation'),
                    'decorators': fn.get('decorators', []),
                    'async': fn.get('async', False),
                    'alternate_definition_lines': ([f.get('line') for f in concrete[:-1]]
                                                   if is_latest else []),
                    'overload_lines': [f.get('line') for f in declarations],
                    'control_depth': fn.get('control_depth', 0),
                    **location(fn),
                })
                if is_latest:
                    local_index[(file_path, qname)] = symbol_id

    # A call inside an earlier function definition belongs to that function,
    # not to a later redefinition bearing the same lexical name.
    scope_definitions: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for s in symbols:
        if s['kind'] in ('FUNCTION', 'METHOD', 'LAMBDA'):
            scope_definitions.setdefault((s['file'], s['qualified_name']), []).append(s)

    def execution_caller(file_path: str, execution_scope: str, call: dict) -> str | None:
        definitions = scope_definitions.get((file_path, execution_scope), [])
        if len(definitions) <= 1:
            return local_index.get((file_path, execution_scope))
        # Containment selects which function body the call was extracted from.
        line = call.get('line') or 0
        containing = [s for s in definitions if
                      (s.get('line') or 0) <= line <= (s.get('end_line') or 0)]
        if containing:
            return max(containing, key=lambda s: s.get('line') or 0)['id']
        return local_index.get((file_path, execution_scope))

    # Class redefinitions may also share a fully qualified name; one stable
    # graph identity per canonical ID is required. Preserve the last binding.
    symbols = list({item['id']: item for item in symbols}.values())

    # 2) import / call / assignment만 추려 저장
    for file_info in ast_result.get("files", []):
        file_path = normalize_path(file_info.get("file", ""))
        module = module_name_from_file(file_path, repository, source_roots)

        for idx, imp in enumerate(file_info.get("imports", [])):
            import_type = imp.get("type")
            imported_module = imp.get("module")
            imported_name = imp.get("name")
            alias = imp.get("alias")

            if import_type == "IMPORT_FROM":
                local_name = alias or imported_name
                imported_canonical = ".".join(
                    x for x in (imported_module, imported_name) if x
                )
            else:
                raw = imported_module or imported_name
                local_name = alias or (raw.split(".")[0] if raw else None)
                imported_canonical = raw if alias else (raw.split(".")[0] if raw else None)

            imports.append({
                "id": f"IMPORT:{file_path}:{imp.get('line')}:{idx}",
                "file": file_path,
                "module": module,
                "type": import_type,
                "imported_module": imported_module,
                "imported_name": imported_name,
                "local_name": local_name,
                "imported_canonical": imported_canonical,
                "scope": imp.get("scope"),
                "level": imp.get("level", 0),
                "control_depth": imp.get("control_depth", 0),
                **location(imp),
            })

        file_calls = []

        for idx, call in enumerate(file_info.get("calls", [])):
            scope = call.get("scope")
            execution_scope = call.get('execution_scope', scope)
            caller_id = execution_caller(file_path, execution_scope, call)

            call_item = {
                "id": f"CALL:{file_path}:{call.get('line')}:{call.get('column')}:{idx}",
                "file": file_path,
                "module": module,
                "caller_id": caller_id,
                "scope": scope,
                "execution_scope": execution_scope,
                "execution_phase": call.get('execution_phase', 'unspecified'),
                "callee_text": call.get("callee_text"),
                "syntax_type": call.get("syntax_type"),
                "positional_argument_count": call.get("positional_argument_count", 0),
                "keyword_arguments": call.get("keyword_arguments", []),
                "source": call.get("source"),
                "flow": call.get("flow"),
                **location(call),
            }
            calls.append(call_item)
            file_calls.append(call_item)

        for hazard in file_info.get("binding_hazards", []):
            binding_hazards.append({"file": file_path, "module": module, **hazard})

        for idx, assignment in enumerate(file_info.get("assignments", [])):
            contained_call_ids = [
                call["id"]
                for call in file_calls
                if call.get("scope") == assignment.get("scope")
                and span_contains(assignment, call)
            ]

            assignments.append({
                "id": f"ASSIGNMENT:{file_path}:{assignment.get('line')}:{assignment.get('column')}:{idx}",
                "file": file_path,
                "module": module,
                "type": assignment.get("type"),
                "targets": assignment.get("targets", []),
                "value": assignment.get("value"),
                "scope": assignment.get("scope"),
                "binding": assignment.get("binding", {"kind": "unknown"}),
                "control_depth": assignment.get("control_depth", 0),
                "contained_call_ids": contained_call_ids,
                **location(assignment),
            })

    return {
        "repository": repository,
        "source_roots": source_roots,
        "analysis_sources": {normalize_path(f['file']): f['source'] for f in ast_result.get('files', []) if 'source' in f},
        "symbol_count": len(symbols),
        "source_modules": sorted({module_name_from_file(normalize_path(f.get("file", "")), repository, source_roots) for f in ast_result.get("files", [])}),
        "overload_declarations": overload_declarations,
        "symbols": symbols,
        "imports": imports,
        "calls": calls,
        "assignments": assignments,
        "binding_hazards": binding_hazards,
    }


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("input", help="ast_result.json")
    parser.add_argument("-o", "--output", default="output/symbol_result.json")
    args = parser.parse_args()

    ast_result = load_json(args.input)
    result = build_symbol_result(ast_result)
    save_json(result, args.output)

    print(f"symbols     : {len(result['symbols'])}")
    print(f"imports     : {len(result['imports'])}")
    print(f"calls       : {len(result['calls'])}")
    print(f"assignments : {len(result['assignments'])}")
    print(f"saved       : {args.output}")


if __name__ == "__main__":
    main()
