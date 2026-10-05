"""Repository-wide, no-execution AST facts for the KnitCode v1 analyzer."""
from __future__ import annotations
import ast
from pathlib import Path
from typing import Any

SKIP_DIRS = {'.git', '.venv', 'venv', '__pycache__', 'node_modules', 'build', 'dist', '.tox', '.mypy_cache', '.pytest_cache', 'output'}


def loc(node: ast.AST) -> dict[str, Any]:
    return {k: getattr(node, v, None) for k, v in (("line", "lineno"), ("column", "col_offset"), ("end_line", "end_lineno"), ("end_column", "end_col_offset"))}


def text_of(source: str, node: ast.AST | None) -> str | None:
    if node is None:
        return None
    return ast.get_source_segment(source, node)


def expr_name(node: ast.AST | None) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        base = expr_name(node.value)
        return f"{base}.{node.attr}" if base else None
    if isinstance(node, ast.Call):
        base = expr_name(node.func)
        return f"{base}()" if base else None
    if isinstance(node, ast.Subscript):
        return expr_name(node.value)
    return None


def binding_of(node: ast.AST | None) -> dict[str, Any]:
    if isinstance(node, ast.Call):
        name = expr_name(node.func)
        return {"kind": "call", "callee": name} if name else {"kind": "unknown"}
    if isinstance(node, ast.Name):
        return {"kind": "name", "name": node.id}
    if isinstance(node, ast.Attribute):
        return {"kind": "attribute", "name": expr_name(node)}
    if isinstance(node, (ast.List, ast.Tuple, ast.Set)):
        names = [expr_name(x) for x in node.elts]
        if all(names):
            return {"kind": "sequence", "items": names}
    return {"kind": "unknown"}


class FactsVisitor(ast.NodeVisitor):
    def __init__(self, source: str):
        self.source = source
        self.scopes: list[str] = []
        self.scope_types: list[str] = []
        self.control_depth = 0
        self.classes: list[dict] = []
        self.functions: list[dict] = []
        self.imports: list[dict] = []
        self.calls: list[dict] = []
        self.assignments: list[dict] = []
        self.binding_hazards: list[dict] = []  # v2.3.1: lexically risky rebinding only

    @property
    def scope(self) -> str:
        return '.'.join(self.scopes) or '<module>'

    def _record_def(self, node: ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef, kind: str):
        qname = '.'.join([*self.scopes, node.name])
        data = {"name": node.name, "qualified_name": qname,
                "scope": self.scope, "control_depth": self.control_depth, "decorators": [text_of(self.source, x) for x in node.decorator_list], **loc(node)}
        if kind == 'CLASS':
            data['bases'] = [expr_name(x) or text_of(self.source, x) for x in node.bases]
            self.classes.append(data)
        else:
            args = (*node.args.posonlyargs, *node.args.args, *node.args.kwonlyargs)
            data.update({"kind": kind, "async": isinstance(node, ast.AsyncFunctionDef),
                         "parameters": [x.arg for x in args],
                         # Only source annotations, never runtime-evaluated hints.
                         "parameter_annotations": {x.arg: text_of(self.source, x.annotation)
                                                   for x in args if x.annotation is not None},
                         "return_annotation": text_of(self.source, node.returns)})
            self.functions.append(data)
        self.scopes.append(node.name)
        self.scope_types.append(kind)
        self.generic_visit(node)
        self.scope_types.pop()
        self.scopes.pop()

    def visit_ClassDef(self, node: ast.ClassDef):
        self._record_def(node, 'CLASS')

    def visit_FunctionDef(self, node: ast.FunctionDef):
        kind = 'METHOD' if self.scope_types and self.scope_types[-1] == 'CLASS' else 'FUNCTION'
        self._record_def(node, kind)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef):
        kind = 'METHOD' if self.scope_types and self.scope_types[-1] == 'CLASS' else 'FUNCTION'
        self._record_def(node, kind)

    def visit_Import(self, node: ast.Import):
        for alias in node.names:
            self.imports.append({"type": "IMPORT", "module": alias.name, "name": None,
                                 "alias": alias.asname, "level": 0, "scope": self.scope,
                                 "control_depth": self.control_depth, **loc(node)})

    def visit_ImportFrom(self, node: ast.ImportFrom):
        for alias in node.names:
            self.imports.append({"type": "IMPORT_FROM", "module": node.module, "name": alias.name,
                                 "alias": alias.asname, "level": node.level, "scope": self.scope,
                                 "control_depth": self.control_depth, **loc(node)})

    def visit_Call(self, node: ast.Call):
        name = expr_name(node.func)
        self.calls.append({"callee_text": name or text_of(self.source, node.func),
                           "syntax_type": 'NAME' if isinstance(node.func, ast.Name) else 'ATTRIBUTE' if isinstance(node.func, ast.Attribute) else 'COMPLEX',
                           "scope": self.scope, "positional_argument_count": len(node.args),
                           "keyword_arguments": [kw.arg for kw in node.keywords],
                           "source": text_of(self.source, node), **loc(node)})
        self.generic_visit(node)

    def _assignment(self, node: ast.AST, targets: list[ast.AST], value: ast.AST | None, typ: str):
        names = [expr_name(x) for x in targets]
        self.assignments.append({"type": typ, "targets": [x for x in names if x],
                                 "value": text_of(self.source, value), "scope": self.scope,
                                 "binding": binding_of(value), "control_depth": self.control_depth,
                                 **loc(node)})

    def visit_Assign(self, node: ast.Assign):
        targets = []
        for target in node.targets:
            if isinstance(target, (ast.Tuple, ast.List)):
                targets.extend(target.elts)
            else:
                targets.append(target)
        self._assignment(node, targets, node.value, 'ASSIGN')
        self.generic_visit(node)

    def visit_AnnAssign(self, node: ast.AnnAssign):
        self._assignment(node, [node.target], node.value, 'ANN_ASSIGN')
        self.generic_visit(node)

    def visit_AugAssign(self, node: ast.AugAssign):
        self._assignment(node, [node.target], None, 'AUG_ASSIGN')
        self.generic_visit(node)

    def visit_For(self, node: ast.For):
        self._visit_for(node)

    def visit_AsyncFor(self, node: ast.AsyncFor):
        self._visit_for(node)

    def _visit_for(self, node: ast.For | ast.AsyncFor):
        names = list(node.target.elts) if isinstance(node.target, (ast.Tuple, ast.List)) else [node.target]
        self.assignments.append({"type": "FOR_TARGET", "targets": [n for n in (expr_name(x) for x in names) if n],
                                 "value": text_of(self.source, node.iter), "scope": self.scope,
                                 "binding": {"kind": "iterate", "source": expr_name(node.iter)},
                                 "control_depth": self.control_depth, **loc(node.target)})
        self.visit(node.iter)
        self.control_depth += 1
        for child in (*node.body, *node.orelse):
            self.visit(child)
        self.control_depth -= 1

    def visit_If(self, node: ast.If):
        self.visit(node.test)
        self.control_depth += 1
        for child in (*node.body, *node.orelse):
            self.visit(child)
        self.control_depth -= 1

    def visit_While(self, node: ast.While):
        self.visit(node.test)
        self.control_depth += 1
        for child in (*node.body, *node.orelse):
            self.visit(child)
        self.control_depth -= 1

    def _hazard(self, node, name, typ):
        if name:
            self.binding_hazards.append({"name": name, "type": typ,
                                         "scope": self.scope,
                                         "control_depth": self.control_depth, **loc(node)})

    def visit_Global(self, node: ast.Global):
        for name in node.names:
            self._hazard(node, name, 'GLOBAL')

    def visit_Nonlocal(self, node: ast.Nonlocal):
        for name in node.names:
            self._hazard(node, name, 'NONLOCAL')

    def visit_Delete(self, node: ast.Delete):
        for target in node.targets:
            self._hazard(node, expr_name(target), 'DELETE')
        self.generic_visit(node)

    def visit_NamedExpr(self, node: ast.NamedExpr):
        self._hazard(node, expr_name(node.target), 'WALRUS')
        self.generic_visit(node)

    def visit_ExceptHandler(self, node: ast.ExceptHandler):
        self._hazard(node, node.name, 'EXCEPT_ALIAS')
        self.generic_visit(node)

    def visit_With(self, node: ast.With):
        for item in node.items:
            self._hazard(node, expr_name(item.optional_vars), 'WITH_ALIAS')
        self.generic_visit(node)

    def visit_AsyncWith(self, node: ast.AsyncWith):
        for item in node.items:
            self._hazard(node, expr_name(item.optional_vars), 'WITH_ALIAS')
        self.generic_visit(node)

    def visit_Try(self, node: ast.Try):
        self.control_depth += 1
        self.generic_visit(node)
        self.control_depth -= 1


def scan_repository(root: str | Path) -> dict[str, Any]:
    root = Path(root).resolve()
    if not root.is_dir():
        raise FileNotFoundError(f"Repository folder not found: {root}")
    files: list[dict] = []
    errors: list[dict] = []
    for file in sorted(root.rglob('*.py')):
        if any(p in SKIP_DIRS for p in file.relative_to(root).parts[:-1]):
            continue
        relative = f"{root.name}/{file.relative_to(root).as_posix()}"
        try:
            source = file.read_text(encoding='utf-8-sig')
            tree = ast.parse(source, filename=str(file))
            visitor = FactsVisitor(source)
            visitor.visit(tree)
            # Optional facts: snapshot abstract variable values at each call
            # without executing any code from the target repository.
            from flow_analysis import FlowAnalyzer
            flow = FlowAnalyzer().analyze(tree)
            for call in visitor.calls:
                snap = flow.get((call.get('line'), call.get('column')))
                if snap is not None:
                    call['flow'] = snap
            files.append({"file": relative, "classes": visitor.classes, "functions": visitor.functions,
                          "imports": visitor.imports, "calls": visitor.calls, "assignments": visitor.assignments,
                          "binding_hazards": visitor.binding_hazards})
        except (SyntaxError, UnicodeError, OSError) as exc:
            errors.append({"file": relative, "type": type(exc).__name__, "message": str(exc)})
    return {"repository": root.name, "root": str(root), "python_file_count": len(files),
            "parse_errors": errors, "files": files}
