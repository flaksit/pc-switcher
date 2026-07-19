# ADR-008: CI Pipeline with Draft-Aware Integration Tests

Status: Accepted
Date: 2025-12-28

## TL;DR
Lint and Unit Tests run on all branches; PR-triggered integration tests skip draft PRs and only start after lint and unit tests pass, ensuring all tests pass before merge to main.

## Implementation Rules
- Integration tests trigger on `pull_request` events
- Skip integration tests on draft PRs (`if: github.event.pull_request.draft == false`)
- Integration tests gate on lint + unit tests: a `wait-for-ci` job blocks on CI's `CI Status` check for the PR head commit (`lewagon/wait-on-check-action`) and `integration` needs it, so the expensive VM job never starts on a red build
- Branch protection requires all three checks: Lint, Unit Tests, Integration Tests

## Context
Integration tests are slow (5-30 min) and use shared VM infrastructure. Originally configured with GitHub merge queues, but this created a chicken-and-egg problem: tests only ran in the merge queue, but PRs couldn't enter the queue without passing tests.

## Decision
- **Fast checks on every push**: Lint and Unit Tests run on all branches
- **Integration tests on ready PRs only**: Triggered by `pull_request` events with draft check
- **Integration gated on fast checks**: The integration job waits for CI's `CI Status` check to pass before running, so a red lint/unit build never provisions VM infrastructure. Waits on the PR head SHA (not the merge commit) and on the aggregate `CI Status` (not `Lint`/`Unit Tests`, which path filtering can skip out of existence)
- **Branch protection gates merge**: All three checks required before merge
- **No merge queue**: Simpler workflow without merge queue complexity

## Consequences

**Positive**:
- Fast iteration on draft PRs (no waiting for integration tests)
- Integration tests run automatically when PR marked ready
- No VM infrastructure wasted on builds that already fail lint or unit tests
- Simple, understandable CI flow
- No merge queue configuration to maintain

**Negative**:
- Integration tests may run multiple times if commits pushed after ready
- Concurrent PRs compete for shared VM infrastructure (mitigated by concurrency group)
- Cross-workflow gating relies on a third-party wait action rather than native `needs:` (lint/unit run on `push` in `ci.yml`, integration on `pull_request`)

## References
- [CI Configuration Details](../ops/ci-setup.md)
- [Testing Guide](../dev/testing-guide.md)
- [ADR-006: VM-Based Testing Framework](adr-006-testing-framework.md)
