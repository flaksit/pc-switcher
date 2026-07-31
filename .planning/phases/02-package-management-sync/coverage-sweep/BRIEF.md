# Coverage sweep brief — common instructions

You are one of eight agents rebuilding the package-sync scenario/coverage matrix from scratch against the current code and the current requirements. Your output is one file; another session assembles the eight into the final document.

## What the matrix is for

Three readers:

1. The developer who writes the tests — needs every branch named, so nothing is left uncovered.
2. The reviewer who validates the tool by running it by hand — needs each scenario stated as an observable situation and an observable outcome.
3. The reviewer who validates by reading code and tests — needs to know which test asserts which branch, and where nothing does.

## The rule that governs your enumeration

**Every logic path and branch the requirements impose must appear as its own row.** Completeness beats brevity. A branch is a distinguishable situation that the requirements say produces a distinguishable outcome. Where an article says "except", "unless", "where X", "either A or B", each side is a branch. Where an article names an ordering, the ordering is a branch. Where an article names a failure mode, the failure is a branch.

Derive branches from the REQUIREMENTS, not from the code's control flow. Then read the code to record what actually happens. Where the code contradicts the requirement, that is a finding, not a reason to reword the row.

Sources, in precedence order:

1. `docs/planning/package-sync-user-requirements.md` — the intent. Wins over everything.
2. `docs/planning/package-sync-conformance-criteria.md` — 130 `PKG-FR-*` / `PKG-NG-*` articles. Your primary enumeration input.
3. `docs/adr/adr-020-declarative-package-convergence.md`, `adr-021-what-the-log-records-and-withholds.md`, `adr-022-broken-tool-fails-fast-bad-data-is-handled.md` — decisions behind the model.
4. `docs/system/package-sync.md` — how it is built. Use to find the code, never as a source of obligations.

Do NOT use `.planning/phases/02-package-management-sync/02-SCENARIO-COVERAGE.md` as your enumeration input. It is stale: it predates a large requirements change and a wholesale restructure of `apt_sync` into a package. You MAY read it to avoid losing a branch someone once found, but every row you emit must be justified from the sources above, and you must not inherit its claims.

## Verifying coverage — read the test, do not trust its name

For every row, find the tests that assert it and **open them**. A test whose name matches the scenario but whose assertions do not reach the branch is NOT coverage. State the verdict:

| Mark | Meaning |
| --- | --- |
| U | a unit test asserts this branch — you read it and it does |
| V | a VM integration test asserts this branch — you read it and it does |
| P | partial — a test is close but one named aspect of the branch is unasserted; say which |
| — | nothing asserts it |
| ‼ | the code does not do what the requirement says, or the requirement is unimplemented |

`U V` where both exist. Integration tests live in `tests/integration/`; everything else is unit.

Evidence is symbol names — `module:TestClass::test_name` — never line numbers. The current unit layout is `tests/unit/jobs/apt/test_apt_*.py` (twelve modules), plus `tests/unit/jobs/test_{snap_sync,flatpak_sync,manual_installs_sync,package_sync_core,package_review,package_state,package_items,block_state_decisions,review_skip_always,folder_sync}.py` and others; integration is `tests/integration/jobs/test_package_sync.py`. Verify paths yourself — do not assume.

## Output format

Write exactly one file: the path given in your task. Structure:

```markdown
# Sweep — <your area>

## <Section letter and title> (articles: PKG-FR-X, PKG-FR-Y)

| # | Scenario | Expected | Cov | Test |
| --- | --- | --- | --- | --- |
| A1 | <the situation, stated as a reviewer could set it up> | <the observable outcome the requirement demands> | U | <module>:`test_name` |

## Gaps

One entry per row marked P, — or ‼. Say what is missing, and whether a unit test with mocks can assert it reliably or it needs a VM (real dpkg/snapd/flatpak semantics). Be specific enough that the next session can write the test without re-deriving anything.

## Notes for the assembler
Rows you split, merged or could not place; branches that overlap another area; anything the requirements leave genuinely ambiguous.
```

Row ids: use the letter prefix you were given and number from 1 upward. Numbering is being reassigned in this pass, so do not try to preserve the old document's numbers — just be dense and sequential.

## Writing style

- Terse. Cut any sentence a reader could skip without acting differently.
- The Scenario column states a situation; the Expected column states an outcome that can be observed. Never describe the implementation there.
- Name machines `Atlas`, `Nomad`, `Vega` if you need names. Never "source machine"/"target machine" in user-facing wording you quote, but you MAY use source/target when describing the sync's roles in your own analysis.
- State as fact only what you verified by reading. Anything inferred, label as inferred.

## Do not

- Do not write or edit any test, any source file, or any document outside your one output file.
- Do not run the test suite (collection is fine if you need it).
- Do not commit.
