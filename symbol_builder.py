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


def module_name_from_file(file_path: str, repository_name: str | None) -> str:
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
        module = module_name_from_file(file_path, repository)
        for cls in file_info.get('classes', []):
            qname = cls.get('qualified_name') or cls.get('name')
            canonical = '.'.join(x for x in (module, qname) if x)
            symbol_id = f"CLASS:{canonical}"
            symbols.append({
                'id': symbol_id, 'kind': 'CLASS', 'name': cls.get('name'),
                'qualified_name': qname, 'canonical_name': canonical,
                'module': module, 'file': file_path, 'scope': cls.get('scope'),
                'bases': cls.get('bases', []), 'decorators': cls.get('decorators', []),
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
            # For repeated non-overload definitions, follow Python's normal
            # straight-line last-binding semantics. Preserve collision evidence.
            fn = concrete[-1]
            symbol_id = f"{kind}:{canonical}"
            symbols.append({
                'id': symbol_id, 'kind': kind, 'name': fn.get('name'),
                'qualified_name': qname, 'canonical_name': canonical,
                'module': module, 'file': file_path, 'scope': fn.get('scope'),
                'parameters': fn.get('parameters', []),
                'parameter_annotations': fn.get('parameter_annotations', {}),
                'return_annotation': fn.get('return_annotation'),
                'decorators': fn.get('decorators', []),
                'async': fn.get('async', False),
                'alternate_definition_lines': [f.get('line') for f in concrete[:-1]],
                'overload_lines': [f.get('line') for f in declarations],
                'control_depth': fn.get('control_depth', 0),
                **location(fn),
            })
            local_index[(file_path, qname)] = symbol_id

    # Class redefinitions may also share a fully qualified name; one stable
    # graph identity per canonical ID is required. Preserve the last binding.
    symbols = list({item['id']: item for item in symbols}.values())

    # 2) import / call / assignment만 추려 저장
    for file_info in ast_result.get("files", []):
        file_path = normalize_path(file_info.get("file", ""))
        module = module_name_from_file(file_path, repository)

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
            caller_id = local_index.get((file_path, scope))

            call_item = {
                "id": f"CALL:{file_path}:{call.get('line')}:{call.get('column')}:{idx}",
                "file": file_path,
                "module": module,
                "caller_id": caller_id,
                "scope": scope,
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
        "symbol_count": len(symbols),
        "source_modules": sorted({module_name_from_file(normalize_path(f.get("file", "")), repository) for f in ast_result.get("files", [])}),
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