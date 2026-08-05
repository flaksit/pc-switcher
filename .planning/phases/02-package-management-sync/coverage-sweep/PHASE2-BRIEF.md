# Phase 2 brief — closing the coverage gaps

You own one part of `docs/dev/package-sync-scenario-coverage.md`. Your job is to make its `Cov` column true by writing the tests it says are missing, and to make the cross-reference work in both directions.

## What to read first

- `docs/dev/package-sync-scenario-coverage.md` — your section's rows, and the `## Gaps — what to test next` register at the end. The register names your rows and, for most, the fixture to set up and the assertion to make.
- `docs/dev/testing-guide.md` — the conventions. Requirement ids go in the **docstring**, never in the test name; the name states the behaviour in plain snake_case.
- `docs/planning/package-sync-conformance-criteria.md` — the article each row decomposes, when the row's wording is not enough.

## What to do

**1. Write the missing tests.** Every row in your section marked `—` or `P` whose gap-register entry says `Unit.` Work through them in row order.

A unit test is right when mocks can establish the branch reliably. Where the branch turns on what real `dpkg`, `apt`, `snapd` or `flatpak` actually does, a unit test asserting our own mock proves nothing — say so and leave the row for a VM test rather than writing a hollow one. Rows whose register entry says `VM.` are not yours: leave them.

It is good to cover several rows with one test where they share a fixture and the assertions are independent — say which rows in the docstring. Completeness first, economy second; never merge rows whose assertions would mask each other.

**2. Tag the tests.** Every test your section's `Test` column names gets its scenario ids at the start of its docstring, in the existing convention:

```python
def test_auto_installed_dependency_produces_no_diff_of_any_kind(self) -> None:
    """A2 — a package apt installed to satisfy a dependency is not an item."""
```

Where a test already carries a requirement id, put the scenario ids first and keep the rest. Where several rows cite one test, name them all (`A2, A3 — …`). This is what makes the matrix checkable from either end, so do it for every test your section cites, not only the ones you write.

**3. Do not touch a row marked `‼`.** Those are defects: the test that proves them belongs with the fix, and writing it now puts a failing test in the suite. Leave the row and its code alone.

**4. Run what you wrote.** `uv run pytest <your modules> -q`, then `uv run ruff format <your modules> && uv run ruff check <your modules>` and `uv run basedpyright <your modules>`. Everything must pass. A test that fails because the code is wrong is a finding — report it, revert that test, and move on; do not change `src/` to make a test pass.

**5. Report, do not commit, and do not edit the matrix.** Another session applies your results to `docs/dev/package-sync-scenario-coverage.md` centrally, because ten agents editing one file lose each other's work.

Return, in this shape:

```
## Rows now covered
A3 — <test you wrote or found>
A53, A58 — <one test covering both>

## Rows left open
A26 — why, and what it would take

## Findings
Anything where the code does not do what the row says, with the symbol and what you saw.
```

Be exact about test symbols: `<module>:<Class>::<test>`. The matrix is only worth what its `Test` column can be checked against.

## File ownership

Stay inside the modules listed in your task. If a test you must tag lives in a module you do not own, report it under `Rows left open` with the symbol — do not edit it. Two agents editing one file lose work.

## Style

Test names state behaviour, not mechanism. Docstrings are one line where one line does. Machines in test fixtures and messages are `Atlas` and `Nomad`. Follow the surrounding module's fixture conventions rather than inventing your own — every apt module has helpers in `tests/unit/jobs/apt/helpers.py`.
