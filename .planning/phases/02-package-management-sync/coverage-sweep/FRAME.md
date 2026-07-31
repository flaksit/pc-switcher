# Package sync — scenario coverage

Every situation the package sync requirements distinguish, with the test that proves each one. Sections A–K enumerate single-run branches; section N composes them into the behaviours that only appear across runs or when the two machines swap roles.

## Navigation

- [Package sync — user requirements](../planning/package-sync-user-requirements.md) — the intent every scenario here comes from
- [Package sync conformance criteria](../planning/package-sync-conformance-criteria.md) — the 130 articles each section decomposes
- [Package sync specification](../system/package-sync.md) — how the behaviour is built
- [Package sync job behaviour](../jobs/package-sync.md) — what the user sees
- [Testing guide](testing-guide.md) — how to write the tests named here

## Who this is for

- **Writing a test**: find the branch, write the test the Cov column says is missing, put the scenario id in the test's docstring.
- **Validating by hand**: the Scenario column is the situation to set up; the Expected column is what to look for. Rows marked `‼` are where the tool knowingly does not do what the requirements say.
- **Validating by reading**: the Test column is the proof. A row with no test is unproven behaviour, whether or not the code looks right.

## How this document is kept true

Scenarios are derived from the requirements, never from the code's control flow. A branch is a situation the requirements say produces a distinguishable outcome: every "except", "unless", "where X", every ordering an article fixes, every failure an article names. Where the code disagrees with an article, the row keeps the article's wording and is marked `‼`.

Where an article forbids something, the scenario asserts the absence. Those rows matter most — nothing else stops a later change from re-introducing what the requirements ruled out.

A scenario id is stable. Tests cite it in their docstring, so renumbering breaks the cross-reference; add new ids at the end of a section instead. Coverage marks state what was verified by reading the test, not what its name suggests.

## Legend

| Mark | Meaning |
| --- | --- |
| U | a unit test asserts this branch |
| V | a VM integration test asserts this branch |
| P | partial — a test is close, but one named aspect is unasserted |
| — | nothing asserts it |
| ‼ | the code does not do what the requirement says, or the requirement is knowingly unmet |

`U V` where both exist. Evidence is symbol names, because the code moves; module shorthands are listed with each part.
