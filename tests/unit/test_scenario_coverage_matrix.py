"""The scenario matrix and the suite must keep agreeing with each other.

`docs/dev/package-sync-scenario-coverage.md` names, for every branch the package sync
requirements impose, the test that proves it; each of those tests names the scenarios it
proves, in the first clause of its docstring. Neither direction is worth anything if the
two drift, and all three ways they drift are silent: a renamed test leaves the matrix
citing nothing, a renumbered scenario leaves a docstring pointing at someone else's
branch, and a tag one character off the shape `DOCSTRING_TAG` reads leaves a proven branch
looking unproven.

These tests are the ratchet. They assert nothing about behaviour — only that the document
and the suite still describe the same tests.
"""

from __future__ import annotations

import ast
import re
from collections import defaultdict
from pathlib import Path

import pytest

MATRIX = Path(__file__).parents[2] / "docs/dev/package-sync-scenario-coverage.md"
TESTS = Path(__file__).parents[1]

#: A row id: section letter, then a number, e.g. `C48`.
SCENARIO_ID = re.compile(r"\b([A-KN]\d{1,3}[a-z]?)\b")
#: A citation in the Test column: `<module>:<Class>::<test>`, module optional.
CITATION = re.compile(r"(Test[A-Za-z0-9_]+)::(test_[a-z0-9_]+)")
#: The opening clause of a tagged docstring: `C48 — ...`, `A2, A3 — ...`, `H2.`
DOCSTRING_TAG = re.compile(r"^\s*((?:[A-KN]\d{1,3}[a-z]?)(?:\s*[,/]\s*[A-KN]\d{1,3}[a-z]?)*)\s*[—:.\-]")
#: A docstring that was MEANT to open with a tag: an id in first position, bare or inside
#: the punctuation a tag is sometimes typed with. Prose that names a row it does not prove
#: puts it mid-sentence, never here.
INTENDED_TAG = re.compile(r"^\s*[`(\[\"'*]*\s*([A-KN]\d{1,3}[a-z]?)\b")

#: Coverage marks that claim a test exists. `—` claims none and `‼` describes a defect
#: whose test lands with its fix, so neither is checked here.
COVERED = {"U", "V", "U V", "P"}

#: Rows that hold by reading two other rows together, so no single test carries their id.
#: Each states its composition in its own Test column; keep that wording if one changes.
COMPOSITIONS = {"D61", "F121", "H133", "J18", "J107", "J129", "K66"}


#: `::test_x` continues the class named just before it; `same test` / `same as C21` carry
#: the previous row's citations. Both keep the table readable and neither is ambiguous in
#: place, so they are resolved here rather than expanded in the document.
SAME_AS = re.compile(r"same (?:test|as)\s*(?:as\s+)?([A-KN]\d{1,3}[a-z]?)?")
#: A continuation, optionally elided (`…::test_x`) and optionally parametrised. It must not
#: also match a full citation, which the alternation below tries first only if it starts at
#: the same offset — so nothing but an ellipsis may sit between the backtick and the `::`.
BARE_TEST = re.compile(r"`\s*(?:…|\.\.\.)?::(test_[a-z0-9_]+)")


def _rows() -> dict[str, tuple[str, str, list[tuple[str, str]]]]:
    """Scenario id -> (coverage mark, Test column, resolved [(Class, test), …])."""
    rows: dict[str, tuple[str, str, list[tuple[str, str]]]] = {}
    previous: list[tuple[str, str]] = []
    for line in MATRIX.read_text().splitlines():
        if not re.match(r"^\| [A-KN]\d", line):
            continue
        cells = [cell.strip() for cell in line.strip().strip("|").split(" | ")]
        if len(cells) != 5:
            continue
        scenario, mark, cited = cells[0], cells[3], cells[4]

        # Walk the cell left to right so a bare `::test_x` binds to the class named
        # nearest before it, not to whichever class the cell happens to end on.
        resolved: list[tuple[str, str]] = []
        current = previous[-1][0] if previous else ""
        for match in re.finditer(f"{CITATION.pattern}|{BARE_TEST.pattern}", cited):
            cls, fn, bare = match.group(1), match.group(2), match.group(3)
            if cls:
                current = cls
                resolved.append((cls, fn))
            elif current:
                resolved.append((current, bare))

        if not resolved and (same := SAME_AS.search(cited)) is not None:
            named = same.group(1)
            resolved = list(rows[named][2]) if named and named in rows else list(previous)

        rows[scenario] = (mark, cited, resolved)
        if resolved:
            previous = resolved
    return rows


def _docstrings() -> list[tuple[str, str, str]]:
    """(Class, method, docstring) for every method of every test class, parsed once."""
    return [
        (node.name, item.name, ast.get_docstring(item) or "")
        for path in sorted(TESTS.rglob("test_*.py"))
        for node in ast.walk(ast.parse(path.read_text()))
        if isinstance(node, ast.ClassDef)
        for item in node.body
        if isinstance(item, ast.FunctionDef | ast.AsyncFunctionDef)
    ]


def _tests(docstrings: list[tuple[str, str, str]]) -> dict[tuple[str, str], set[str]]:
    """(Class, test) -> every scenario id any module's copy of it claims.

    Keyed without the module because the Test column often omits it, and a class and test
    name together are specific enough in practice. Where two modules share both — several
    have a `TestValidate` — their ids are pooled, which is the lenient direction.
    """
    found: dict[tuple[str, str], set[str]] = defaultdict(set)
    for cls, fn, doc in docstrings:
        tag = DOCSTRING_TAG.match(doc)
        found[(cls, fn)] |= set(SCENARIO_ID.findall(tag.group(1))) if tag else set()
    return found


ROWS = _rows()
DOCSTRINGS = _docstrings()
SUITE = _tests(DOCSTRINGS)


CLAIMED = sorted(sid for sid, (mark, _, _) in ROWS.items() if mark in COVERED)


def test_the_matrix_is_well_formed() -> None:
    """Every row carries five columns, a unique id and a coverage mark from the legend."""
    assert len(ROWS) > 1000, f"only {len(ROWS)} rows parsed — the table shape has changed"
    unknown = {sid: mark for sid, (mark, _, _) in ROWS.items() if mark not in COVERED | {"—", "‼"}}
    assert not unknown, f"coverage marks outside the legend: {unknown}"


def test_no_scenario_states_which_tier_proves_it() -> None:
    """The Scenario and Expected columns describe a situation and its outcome, not a test.

    Which tier proves a row is the Cov column's job, and saying it twice is how "on a VM"
    ends up in a sentence a reviewer is meant to set up by hand. Where a branch genuinely
    needs real package-manager behaviour, the Expected column says so by naming what a real
    run prints — `apt-get update` exiting 0, say — rather than by naming the test.
    """
    tiers = re.compile(r"\b(on a VM|VM run|integration test|unit test|mocked|a real run on a VM)\b")
    offenders: dict[str, str] = {}
    for line in MATRIX.read_text().splitlines():
        if not re.match(r"^\| [A-KN]\d", line):
            continue
        cells = [cell.strip() for cell in line.strip().strip("|").split(" | ")]
        if len(cells) == 5 and (hit := tiers.search(f"{cells[1]} {cells[2]}")):
            offenders[cells[0]] = hit.group(0)
    assert not offenders, f"rows name a test tier in the Scenario or Expected column: {offenders}"


def test_every_claim_of_coverage_names_a_test() -> None:
    """A row claiming coverage must say which test proves it."""
    silent = [sid for sid in CLAIMED if not ROWS[sid][2] and sid not in COMPOSITIONS]
    assert not silent, (
        f"rows claim coverage but name no test: {silent}. Cite a `Class::test`, or list the row"
        f" in COMPOSITIONS where its branch holds only by reading two other rows together."
    )


@pytest.mark.parametrize("scenario", CLAIMED)
def test_every_cited_test_exists(scenario: str) -> None:
    """A row claiming coverage must cite a test the suite actually has."""
    missing = [f"{cls}::{fn}" for cls, fn in ROWS[scenario][2] if (cls, fn) not in SUITE]
    assert not missing, f"{scenario} cites tests that do not exist: {missing}"


@pytest.mark.parametrize("scenario", CLAIMED)
def test_every_cited_test_names_the_scenario_back(scenario: str) -> None:
    """The cross-reference runs both ways: a cited test carries the id that cites it.

    Without this, a scenario can be renumbered and every docstring left pointing at the
    branch that now holds its old number — which reads as coverage and is not.
    """
    citations = ROWS[scenario][2]
    if scenario in COMPOSITIONS or not citations:
        return
    if any(scenario in SUITE.get(key, set()) for key in citations):
        return
    named = [f"{cls}::{fn}" for cls, fn in citations]
    raise AssertionError(
        f"{scenario} is cited by no test that names it back."
        f" Put `{scenario}` at the start of the docstring of one of: {named}"
    )


def test_no_docstring_opens_with_a_tag_the_matcher_cannot_read() -> None:
    """A docstring that starts with a scenario id must parse as one.

    `DOCSTRING_TAG` reads ids, comma-separated, then a dash — so a single character out of
    place breaks it: an article name backticked in among the ids, an "and" where a comma
    belongs. The tag then goes unseen, and nothing else notices. The suite still passes, the
    row it was written for still reads `—`, and a branch a passing test already proves reads
    as work outstanding. That is the drift direction nobody investigates, because it
    understates coverage rather than claiming any.

    Prose may name a row it does not prove — "distinguished from C21 by …", "the other half
    of B14" — so this fires only on an id in first position, the one place a tag ever sits.
    The whole cost is that a docstring may not OPEN with a bare row id it means as prose;
    lead with the sentence instead and the id can appear anywhere after.
    """
    unreadable = {
        f"{cls}::{fn}": intended.group(1)
        for cls, fn, doc in DOCSTRINGS
        if not DOCSTRING_TAG.match(doc) and (intended := INTENDED_TAG.match(doc))
    }
    assert not unreadable, (
        f"docstrings open with a scenario id that is not a readable tag: {unreadable}."
        f" Write the ids first, separated by commas, then `—`, then the prose:"
        f' `"""C48, C49 — …"""`. Anything else in the opening clause leaves the row'
        f" reading as unproven."
    )


def test_no_test_claims_a_scenario_the_matrix_does_not_have() -> None:
    """A docstring id that matches no row is a stale tag left by a renumbering."""
    orphans: dict[str, list[str]] = defaultdict(list)
    for (cls, fn), ids in SUITE.items():
        for sid in ids - set(ROWS):
            orphans[sid].append(f"{cls}::{fn}")
    assert not orphans, f"tests name scenarios the matrix does not define: {dict(orphans)}"
