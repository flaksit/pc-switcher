# ADR-008: CI Pipeline with Draft-Aware Integration Tests

Status: Accepted
Date: 2025-12-28

## TL;DR
Lint and Unit Tests run on all branches and PR-triggered integration tests that skip draft PRs while ensuring all tests pass before merge to main.

## Implementation Rules
- Integration tests trigger on `pull_request` events
- Skip integration tests on draft PRs (`if: github.event.pull_request.draft == false`)
- Branch protection requires all three checks: Lint, Unit Tests, Integration Tests

## Context
Integration tests are slow (5-30 min) and use shared VM infrastructure. Originally configured with GitHub merge queues, but this created a chicken-and-egg problem: tests only ran in the merge queue, but PRs couldn't enter the queue without passing tests.

## Decision
- **Fast checks on every push**: Lint and Unit Tests run on all branches
- **Integration tests on ready PRs only**: Triggered by `pull_request` events with draft check
- **Branch protection gates merge**: All three checks required before merge
- **No merge queue**: Simpler workflow without merge queue complexity

## Consequences

**Positive**:
- Fast iteration on draft PRs (no waiting for integration tests)
- Integration tests run automatically when PR marked ready
- Simple, understandable CI flow
- No merge queue configuration to maintain

**Negative**:
- Integration tests may run multiple times if commits pushed after ready
- Concurrent PRs compete for shared VM infrastructure (mitigated by concurrency group)

## References
- [CI Configuration Details](../ci-configuration.md)
- [Testing Developer Guide](../testing-developer-guide.md)
- [ADR-006: VM-Based Testing Framework](adr-006-testing-framework.md)
