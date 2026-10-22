"""Compare KnitCode CALLS/INSTANTIATES across unstable visitor call-id indexes.

Run from the *new* KnitCode_v25_execution_context project directory:
    python C:\\Users\\c\\Downloads\\compare_execution_context_stable_v2.py

Optional: pass the former source-root project folder as the first argument.
The script reads graph_result.json files, never edits them, and writes 3 CSVs
in the current directory. Source coordinate collisions are handled and flagged.
"""
from __future__ import annotations

import csv
import json
import sys
from collections import defaultdict
from pathlib import Path

DEFAULT_OLD_ROOT = Path(r"C:\Users\c\Downloads\KnitCode_v25_source_root_fix\KnitCode_v25_source_root")
PROJECTS = ("sublist3r", "requests", "click")
TYPES = {"CALLS", "INSTANTIATES"}
COLUMNS = (
    "change", "file", "line", "column", "expression", "old_call_ids", "new_call_ids",
    "old_relations", "new_relations", "old_phases", "new_phases",
    "same_position_multiple_calls", "match_basis",
)


def extract(graph):
    # One graph edge per relation; several edges can refer to a single call ID.
    by_id = {}
    for edge in graph.get("edges", []):
        if edge.get("type") not in TYPES:
            continue
        evidence = edge.get("evidence") or {}
        call_id = evidence.get("call_id")
        if not call_id:
            continue
        key = (evidence.get("file"), evidence.get("line"), evidence.get("column"))
        if None in key:
            raise ValueError(f"Missing source position for {call_id}")
        expression = evidence.get("source") or ""
        record = by_id.setdefault(call_id, {
            "position": key, "expression": expression, "relations": set(), "phases": set(),
            "call_id": call_id,
        })
        if record["position"] != key or record["expression"] != expression:
            raise ValueError(f"Inconsistent source evidence for call ID: {call_id}")
        record["relations"].add((edge["source"], edge["target"], edge["type"]))
        if evidence.get("execution_phase"):
            record["phases"].add(evidence["execution_phase"])
    groups = defaultdict(list)
    for record in by_id.values():
        groups[record["position"]].append(record)
    for group in groups.values():
        group.sort(key=lambda r: r["call_id"])
    return groups, len(by_id)


def targets(site):
    return frozenset((target, kind) for _, target, kind in site["relations"])


def callers(site):
    return frozenset((source, kind) for source, _, kind in site["relations"])


def match_group(old_group, new_group):
    """Match individual calls at the same position; never rely on trailing idx.

    Find strongest matches first. A collision with indistinguishable calls is
    flagged in output: this is *not* independent proof of identity.
    """
    before = list(old_group)
    after = list(new_group)
    pairs = []

    tiers = (
        ("exact_expression_and_relations", lambda a,b: a["expression"] == b["expression"] and a["relations"] == b["relations"]),
        ("same_expression_and_targets", lambda a,b: a["expression"] == b["expression"] and targets(a) == targets(b)),
        ("same_expression_and_callers", lambda a,b: a["expression"] == b["expression"] and callers(a) == callers(b)),
        ("same_expression", lambda a,b: a["expression"] == b["expression"]),
        # Rare situation where generated source text differs despite same position.
        ("position_only_review", lambda a,b: True),
    )

    for basis, predicate in tiers:
        while True:
            candidates = [(i,j) for i,a in enumerate(before) for j,b in enumerate(after)
                          if predicate(a,b)]
            if not candidates:
                break
            # Prefer closest phase and relation size if candidates are tied.
            i,j = min(candidates, key=lambda ij: (
                before[ij[0]]["phases"] != after[ij[1]]["phases"],
                abs(len(before[ij[0]]["relations"]) - len(after[ij[1]]["relations"])),
                before[ij[0]]["call_id"], after[ij[1]]["call_id"],
            ))
            pairs.append((before.pop(i), after.pop(j), basis))
    for item in before:
        pairs.append((item, None, "unmatched"))
    for item in after:
        pairs.append((None, item, "unmatched"))
    return pairs


def as_json(value):
    return json.dumps(value, ensure_ascii=False)


def compare(old_graph, new_graph):
    if old_graph.get("repository") != new_graph.get("repository"):
        raise ValueError("Repository mismatch; check the input paths")
    if old_graph.get("commit") and new_graph.get("commit") and old_graph["commit"] != new_graph["commit"]:
        raise ValueError("Git commit mismatch; results are not comparable")

    old, old_count = extract(old_graph)
    new, new_count = extract(new_graph)
    totals = defaultdict(int)
    details = []
    all_positions = sorted(set(old) | set(new))
    collision_positions = sum(len(old.get(pos, [])) > 1 or len(new.get(pos, [])) > 1
                              for pos in all_positions)
    changed_collision = 0
    for pos in all_positions:
        old_group, new_group = old.get(pos, []), new.get(pos, [])
        duplicate = len(old_group) > 1 or len(new_group) > 1
        pairs = match_group(old_group, new_group)
        if duplicate and (len(old_group) != len(new_group) or any(
            a is None or b is None or a["relations"] != b["relations"] for a,b,_ in pairs
        )):
            changed_collision += 1
        for a,b,basis in pairs:
            if a is None:
                change = "newly_resolved"
            elif b is None:
                change = "lost_resolution"
            elif a["relations"] == b["relations"]:
                change = "unchanged"
            elif targets(a) == targets(b):
                change = "caller_only"
            elif callers(a) == callers(b):
                change = "target_only"
            else:
                change = "caller_and_target"
            totals[change] += 1
            if change == "unchanged" and not duplicate:
                continue
            details.append({
                "change": change,
                "file": pos[0], "line": pos[1], "column": pos[2],
                "expression": (b or a)["expression"],
                "old_call_ids": a["call_id"] if a else "",
                "new_call_ids": b["call_id"] if b else "",
                "old_relations": as_json(sorted(a["relations"])) if a else "[]",
                "new_relations": as_json(sorted(b["relations"])) if b else "[]",
                "old_phases": ",".join(sorted(a["phases"])) if a else "",
                "new_phases": ",".join(sorted(b["phases"])) if b else "",
                "same_position_multiple_calls": str(duplicate),
                "match_basis": basis,
            })
    totals.update({
        "old_resolved": old_count,
        "new_resolved": new_count,
        "collision_positions": collision_positions,
        "changed_collision_positions": changed_collision,
        "module_level_before": old_graph.get("module_level_resolved_sites"),
        "module_level_after": new_graph.get("module_level_resolved_sites"),
    })
    if sum(totals[name] for name in ("unchanged","caller_only","target_only","caller_and_target","lost_resolution")) != old_count:
        raise AssertionError("Old resolved call accounting mismatch")
    if sum(totals[name] for name in ("unchanged","caller_only","target_only","caller_and_target","newly_resolved")) != new_count:
        raise AssertionError("New resolved call accounting mismatch")
    return totals, details


def main():
    old_root = Path(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_OLD_ROOT
    new_root = Path.cwd()
    for project in PROJECTS:
        old_file = old_root / f"output_{project}_source_root" / "graph_result.json"
        new_file = new_root / f"output_{project}_context" / "graph_result.json"
        print(f"\n=== {project.upper()} ===", flush=True)
        if not old_file.is_file() or not new_file.is_file():
            print(f"SKIP: old_exists={old_file.is_file()}, new_exists={new_file.is_file()}")
            continue
        try:
            before = json.loads(old_file.read_text(encoding="utf-8-sig"))
            after = json.loads(new_file.read_text(encoding="utf-8-sig"))
            totals, rows = compare(before, after)
            filename = Path(f"stable_context_diff_{project}.csv")
            with filename.open("w", encoding="utf-8-sig", newline="") as f:
                writer = csv.DictWriter(f, fieldnames=COLUMNS)
                writer.writeheader()
                writer.writerows(rows)
        except (ValueError, KeyError, TypeError, OSError, json.JSONDecodeError) as exc:
            print(f"ERROR: {type(exc).__name__}: {exc}")
            continue
        for key in ("old_resolved","new_resolved","unchanged","caller_only",
                    "target_only","caller_and_target","newly_resolved","lost_resolution",
                    "collision_positions","changed_collision_positions",
                    "module_level_before","module_level_after"):
            print(f"{key}: {totals.get(key,0)}")
        print(f"CSV: {filename.resolve()}")
        for row in [r for r in rows if r["change"] in ("lost_resolution","caller_only")][:3]:
            print(f"  {row['change']}: {row['file']}:{row['line']}:{row['column']} {row['expression'][:65]!r}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
