#!/usr/bin/env python
"""Spec reference checker — spec/0-index.md ("Integrity at this revision").

Fails on any duplicate requirement ID, dangling reference, unassigned
buildable requirement, or a range (``X-Y``) in a milestone ``Implements:``
line, and regenerates ``coverage-report.md``. Run after every spec edit.

Requirement IDs are defined by:
- headings  ``## ID · Title`` or ``### ID · Title``  (GATE sub-requirements are H3)
- family table rows ``| ID | Title | ...`` (the STRAT-010..013 family table)

Usage:  python spec/tools/check_references.py   (from repo root)
"""

from __future__ import annotations

import re
import sys
from collections import Counter
from pathlib import Path

SPEC_DIR = Path(__file__).resolve().parent.parent

DOCS = {
    "1-product-requirements.md": "PROD",
    "2-research-protocol.md": "GATE, VAL",
    "3-architecture.md": "ARCH, DATA, FEAT, COST, BT, STRAT, RISK, CHAL, EXEC",
    "4-milestones.md": "MILE",
    "5-operations-runbook.md": "OPS, SEC",
}

PREFIXES = (
    "PROD|GATE|VAL|ARCH|DATA|FEAT|COST|STRAT|RISK|CHAL|EXEC|MILE|OPS|SEC|BT"
)
ID_PATTERN = rf"(?:{PREFIXES})-\d{{2,4}}[a-z]?"

# Heading definitions: "## ID · Title" and "### ID · Title".
HEADING_DEF_RE = re.compile(rf"^#{{2,3}}\s+({ID_PATTERN})\s*[·•-]\s*(.+?)\s*$", re.MULTILINE)
# Family-table definitions: "| `ID` | Title | ..." (cell decorations allowed).
TABLE_DEF_RE = re.compile(rf"^\|\s*[`*_\s]*({ID_PATTERN})[`*_\s]*\|", re.MULTILINE)
# Any ID token anywhere in the spec text.
ANY_ID_RE = re.compile(ID_PATTERN)
IMPLEMENTS_RE = re.compile(r"\*\*Implements:\*\*\s*(.+)")
RANGE_TOKEN_RE = re.compile(
    rf"\b({ID_PATTERN})\s*(?:–|—|to)\s*`?([A-Z]+-\d{{2,4}}[a-z]?)\b"
)

# Non-buildable requirements: milestones themselves (doc 4), plus the charter
# and process-governance requirements that carry no construction artefact and
# that document 4 deliberately leaves unassigned. Anything unassigned outside
# this set fails the check.
NON_BUILDABLE_PREFIXES = ("MILE",)
NON_BUILDABLE_IDS = {
    "PROD-001",  # charter: what the project is
    "PROD-002",  # charter: what it is not
    "PROD-003",  # charter: automation boundary
    "PROD-012",  # scenario risks carried explicitly
    "PROD-013",  # regulatory context note
    "PROD-020",  # scope statement
    "PROD-022",  # the three unknowns (owned by the gates)
    "PROD-030",  # known weaknesses of the specification
    "PROD-031",  # claims deliberately not made
    "OPS-031",   # disk capacity governance
    "OPS-032",   # ledger integrity governance
    "OPS-033",   # schema/config migration governance
    "OPS-041",   # retention policy
    "OPS-050",   # declared-constants governance
}
# MILE-002's definition-of-ready checklist assigns schema/build work by name.
CHECKBOX_RE = re.compile(r"^- \[[ xX]\] .*$", re.MULTILINE)


def main() -> int:
    texts: dict[str, str] = {}
    for name in list(DOCS) + ["0-index.md", "coverage-report.md"]:
        path = SPEC_DIR / name
        if path.exists():
            texts[name] = path.read_text(encoding="utf-8")

    # 1. Define every ID exactly once.
    definitions: dict[str, str] = {}
    duplicates: list[str] = []
    for name, prefix_decl in DOCS.items():
        if name not in texts:
            print(f"FAIL: missing spec document {name}")
            return 1
        allowed = {p.strip() for p in prefix_decl.split(",")}
        found: dict[str, str] = {}
        for match in HEADING_DEF_RE.finditer(texts[name]):
            found[match.group(1)] = "heading"
        for match in TABLE_DEF_RE.finditer(texts[name]):
            # A table row defines only if no heading already defined the ID.
            found.setdefault(match.group(1), "table")
        for rid in found:
            prefix = rid.split("-")[0]
            if prefix not in allowed:
                print(
                    f"FAIL: {name} defines {rid} but its declared prefixes are '{prefix_decl}'"
                )
                return 1
        for rid in found:
            if rid in definitions:
                duplicates.append(rid)
            else:
                definitions[rid] = name
    if duplicates:
        print(f"FAIL: duplicate requirement IDs: {sorted(duplicates)}")
        return 1

    # 2. Collect references outside the defining heading.
    references: Counter[str] = Counter()
    dangling: list[str] = []
    for name, text in texts.items():
        body = HEADING_DEF_RE.sub("", text)
        for match in ANY_ID_RE.finditer(body):
            rid = match.group(0)
            if rid in definitions and definitions[rid] != name:
                references[rid] += 1
            elif rid not in definitions:
                dangling.append(rid)
    dangling = sorted(set(dangling))
    if dangling:
        print(f"FAIL: dangling references to undefined IDs: {dangling}")
        return 1

    # 3. Milestones: no ranges on Implements lines; every buildable ID assigned.
    ranges_found: list[str] = []
    assigned: set[str] = set()
    milestones_text = texts["4-milestones.md"]
    # Definition-of-ready checkboxes also assign build work by name (MILE-002).
    for line in CHECKBOX_RE.findall(milestones_text):
        assigned.update(ANY_ID_RE.findall(line))
    for m in IMPLEMENTS_RE.finditer(milestones_text):
        implements_line = m.group(1)
        assigned.update(ANY_ID_RE.findall(implements_line))
        for rm in RANGE_TOKEN_RE.finditer(implements_line):
            ranges_found.append(f"{rm.group(1)}-{rm.group(2)}")
    if ranges_found:
        print(f"FAIL: ranges in milestone Implements lines are not allowed: {ranges_found}")
        return 1

    # 4. Unassigned buildable requirements.
    buildable = {
        rid
        for rid in definitions
        if not rid.startswith(NON_BUILDABLE_PREFIXES) and rid not in NON_BUILDABLE_IDS
    }
    unassigned = sorted(buildable - assigned)
    if unassigned:
        print(f"FAIL: buildable requirements not assigned to any milestone: {unassigned}")
        return 1
    unknown_assigned = sorted(assigned - set(definitions))
    if unknown_assigned:
        print(f"FAIL: milestones reference undefined IDs: {unknown_assigned}")
        return 1

    # 5. Regenerate coverage-report.md.
    lines = [
        "# Coverage report",
        "",
        "Generated by `spec/tools/check_references.py` — do not edit by hand.",
        "",
        f"- Requirement IDs defined: {len(definitions)}",
        f"- Referenced (outside their defining document): {len(references)}",
        "- Duplicates: 0",
        "- Dangling references: 0",
        f"- Buildable requirements assigned to a milestone: "
        f"{len(buildable & assigned)} of {len(buildable)}",
        "",
        "| Requirement | Defined in | Referenced | Milestone-assigned |",
        "| --- | --- | --- | --- |",
    ]
    for rid in sorted(definitions):
        lines.append(
            f"| {rid} | {definitions[rid]} | {'yes' if references[rid] else 'no'} "
            f"| {'yes' if rid in assigned or rid.startswith(NON_BUILDABLE_PREFIXES) or rid in NON_BUILDABLE_IDS else 'no'} |"
        )
    (SPEC_DIR / "coverage-report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")

    print(
        f"OK: {len(definitions)} IDs defined, {len(references)} referenced, "
        f"0 dangling, 0 duplicates, {len(buildable & assigned)} of {len(buildable)} "
        "buildable requirements assigned. coverage-report.md regenerated."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
