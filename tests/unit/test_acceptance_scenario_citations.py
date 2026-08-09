"""Every acceptance scenario a test or a source file cites must exist in `docs/system/`.

A scenario declares its own id in the document text (`- **CORE-US-TUI-AS2**: **Given** …`),
allocated append-only, so a citation names an article rather than a position in a list. This
file asserts the one thing that can still go wrong under that scheme: a citation naming an id
no document declares — a story renamed, a scenario deleted, a number typed wrong.

What it does NOT catch: whether a citation is about the RIGHT scenario. `CORE-US-TUI-AS2`
resolving proves AS2 exists, not that the citing test exercises it. That reading is a human's.
What it no longer has to catch is the drift that made this file necessary: while the numbers
were list positions, inserting a scenario silently repointed every citation at or below it,
and every one of them still resolved. Explicit append-only ids remove that failure entirely —
an inserted scenario takes a fresh number and moves nobody else's.
"""

from __future__ import annotations

import re
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).parents[2]
DOCS = ROOT / "docs/system"
#: Where citations are written. Documentation cites scenarios too, but only these two carry
#: the claims of coverage a stale citation would misrepresent.
SOURCES = (ROOT / "tests", ROOT / "src")

#: A declared scenario: the id in bold, opening its own bullet.
DECLARATION = re.compile(r"^- \*\*([A-Z]{2,6}-US-[A-Z0-9-]+-AS\d+)\*\*:", re.MULTILINE)
#: A citation, with any `/AS3` continuations that follow it — `CORE-US-BTRFS-AS2/AS3` cites
#: two scenarios of one story, and dropping the second would leave it unchecked.
CITATION = re.compile(r"\b(?P<story>[A-Z]{2,6}-US-(?:[A-Z0-9]+-)+)AS(?P<first>\d+)(?P<more>(?:/AS\d+)+)?\b")


def _declared() -> set[str]:
    """Every scenario id the system documents declare."""
    return {
        scenario
        for path in sorted(DOCS.glob("*.md"))
        for scenario in DECLARATION.findall(path.read_text(encoding="utf-8"))
    }


def _cited() -> dict[str, list[str]]:
    """Scenario id -> the `<path>:<line>` of every citation of it."""
    found: dict[str, list[str]] = defaultdict(list)
    for root in SOURCES:
        for path in sorted(root.rglob("*")):
            if path.suffix not in {".py", ".md"} or not path.is_file():
                continue
            for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
                for match in CITATION.finditer(line):
                    story, more = match.group("story"), match.group("more") or ""
                    for suffix in [match.group("first"), *re.findall(r"\d+", more)]:
                        found[f"{story}AS{suffix}"].append(f"{path.relative_to(ROOT)}:{number}")
    return found


DECLARED = _declared()
CITED = _cited()


def test_the_documents_declare_scenarios_this_file_can_read() -> None:
    """The parsers must find work to do, and no id may be declared twice.

    Both floors guard against a silently vacuous run: a change of rendering that stops
    `DECLARATION` matching leaves every citation resolving against an empty set, and a change
    of citation style leaves nothing to resolve. A duplicated id is the append-only rule
    broken — two scenarios answering to one citation, with no way to tell which was meant.
    """
    assert len(DECLARED) > 50, f"only {len(DECLARED)} scenarios parsed — the declaration format has changed"
    assert len(CITED) > 20, f"only {len(CITED)} scenarios cited — the citation format has changed"

    declarations = [
        match.group(1)
        for path in sorted(DOCS.glob("*.md"))
        for match in DECLARATION.finditer(path.read_text(encoding="utf-8"))
    ]
    repeated = sorted({scenario for scenario in declarations if declarations.count(scenario) > 1})
    assert not repeated, (
        f"scenario ids declared more than once: {repeated}. Numbers are append-only: give the"
        f" newer scenario the next unused number for its story instead."
    )


def test_every_cited_scenario_exists() -> None:
    """A citation must name a scenario `docs/system/` actually declares."""
    unresolved = {scenario: sorted(set(where)) for scenario, where in CITED.items() if scenario not in DECLARED}
    assert not unresolved, (
        f"citations name scenarios no document declares: {unresolved}. Either the id is"
        f" mistyped, or the scenario was deleted and its citation left behind — retired"
        f" numbers are never reused, so the citation cannot be repointed by renumbering."
    )
