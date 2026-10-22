"""Compare KnitCode graph results across AST traversal-order changes.

Run from the KnitCode_v25_execution_context project directory:
    python C:\\Users\\c\\Downloads\\compare_execution_context_stable.py

Unlike compare_results.py, this matches call sites by (file, line, column),
not the unstable trailing per-file visitor index in CALL:... identifiers.
"""
from __future__ import annotations

import csv
import json
import sys
from collections import defaultdict
from pathlib import Path

DEFAULT_OLD_ROOT = Path(r"C:\Users\c\Downloads\KnitCode_v25_source_root_fix\KnitCode_v25_source_root")
PROJECTS = ("sublist3r", "requests", "click")


def extract(graph: dict) -> dict:
    sites: dict[tuple, dict] = {}
    seen_ids: dict[tuple, set[str]] = defaultdict(set)
    for edge in graph.get("edges", []):
        if edge.get("type") not in ("CALLS", "INSTANTIATES"):
            continue
        ev = edge.get("evidence") or {}
        cid = ev.get("call_id")
        if not cid:
            continue
        key = (ev.get("file"), ev.get("line"), ev.get("column"))
        if None in key:
            raise ValueError(f"Call evidence lacks stable file/line/column: {cid}")
        seen_ids[key].add(cid)
        entry = sites.setdefault(key, {
            "text": ev.get("source") or "",
            "relations": set(),
            "phases": set(),
        })
        entry["relations"].add((edge.get("source"), edge.get("target"), edge.get("type")))
        if ev.get("execution_phase"):
            entry["phases"].add(ev["execution_phase"])
    ambiguous = [(key, ids) for key, ids in seen_ids.items() if len(ids) != 1]
    if ambiguous:
        raise ValueError(f"Multiple call IDs occupy identical source location: {ambiguous[:3]}")
    return sites


def relations_as_json(site: dict | None) -> str:
    if site is None:
        return "[]"
    return json.dumps(sorted(site["relations"]), ensure_ascii=False)


def compare(old_graph: dict, new_graph: dict) -> tuple[dict, list[dict]]:
    old_repo, new_repo = old_graph.get("repository"), new_graph.get("repository")
    if old_repo != new_repo:
        raise ValueError(f"Repository mismatch: {old_repo!r} vs {new_repo!r}")
    old_sha, new_sha = old_graph.get("commit"), new_graph.get("commit")
    if old_sha and new_sha and old_sha != new_sha:
        raise ValueError(f"Commit mismatch: {old_sha} vs {new_sha}")
    old = extract(old_graph)
    new = extract(new_graph)
    counts = defaultdict(int)
    rows = []
    for key in sorted(old.keys() | new.keys()):
        a, b = old.get(key), new.get(key)
        if a is None:
            change = "newly_resolved"
        elif b is None:
            change = "lost_resolution"
        elif a["relations"] == b["relations"]:
            change = "unchanged"
        else:
            before_targets = {(t, kind) for _, t, kind in a["relations"]}
            after_targets = {(t, kind) for _, t, kind in b["relations"]}
            before_callers = {source for source, _, _ in a["relations"]}
            after_callers = {source for source, _, _ in b["relations"]}
            if before_targets == after_targets:
                change = "caller_only"
            elif before_callers == after_callers:
                change = "target_only"
            else:
                change = "caller_and_target"
        counts[change] += 1
        if change != "unchanged":
            rows.append({
                "change": change,
                "file": key[0], "line": key[1], "column": key[2],
                "expression": (b or a)["text"],
                "old_relations": relations_as_json(a),
                "new_relations": relations_as_json(b),
                "old_phases": ",".join(sorted(a["phases"])) if a else "",
                "new_phases": ",".join(sorted(b["phases"])) if b else "",
            })
    counts["old_resolved"] = len(old)
    counts["new_resolved"] = len(new)
    counts["total_call_sites_before"] = old_graph.get("total_call_sites")
    counts["total_call_sites_after"] = new_graph.get("total_call_sites")
    counts["module_level_before"] = old_graph.get("module_level_resolved_sites")
    counts["module_level_after"] = new_graph.get("module_level_resolved_sites")
    return dict(counts), rows


def main() -> int:
    # Optionally provide the old project directory as the first CLI argument.
    old_root = Path(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_OLD_ROOT
    new_root = Path.cwd()
    for project in PROJECTS:
        old_file = old_root / f"output_{project}_source_root" / "graph_result.json"
        new_file = new_root / f"output_{project}_context" / "graph_result.json"
        print(f"\n=== {project.upper()} (stable source location) ===")
        if not old_file.is_file() or not new_file.is_file():
            print(f"SKIP: old_exists={old_file.is_file()}, new_exists={new_file.is_file()}")
            continue
        try:
            old = json.loads(old_file.read_text(encoding="utf-8-sig"))
            new = json.loads(new_file.read_text(encoding="utf-8-sig"))
            counts, rows = compare(old, new)
        except (ValueError, KeyError, TypeError) as exc:
            print(f"ERROR: {exc}")
            continue
        for label in (
            "old_resolved", "new_resolved", "unchanged", "caller_only",
            "target_only", "caller_and_target", "newly_resolved", "lost_resolution",
            "module_level_before", "module_level_after",
        ):
            print(f"{label}: {counts.get(label, 0)}")
        out = Path(f"stable_context_diff_{project}.csv")
        with out.open("w", encoding="utf-8-sig", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=(
                "change", "file", "line", "column", "expression", "old_relations",
                "new_relations", "old_phases", "new_phases",
            ))
            writer.writeheader()
            writer.writerows(rows)
        print(f"Detailed changes: {out}")
        for label in ("caller_only", "target_only", "caller_and_target", "lost_resolution"):
            filtered = [r for r in rows if r["change"] == label]
            for r in filtered[:2]:
                print(f"  {label}: {r['file']}:{r['line']}:{r['column']} {r['expression'][:90]!r}")
                print(f"    before={r['old_relations'][:270]}")
                print(f"    after ={r['new_relations'][:270]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
