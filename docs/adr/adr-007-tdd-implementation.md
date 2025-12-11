# ADR-007: Test-Driven Development for Implementation

Status: Accepted
Date: 2025-12-11

## TL;DR
Use Test-Driven Development (TDD) during SpecKit implementation phases to ensure correctness and maintain living documentation.

## Implementation Rules
- Write a failing test before implementing any new functionality
- Follow the red-green-refactor cycle: failing test → minimal implementation → refactor
- Tests MUST be derived from task specifications in `tasks.md`
- Each task in `/speckit.implement` MUST start with test creation
- Refactoring MUST NOT change behavior (tests must stay green)
- Skip TDD only for trivial changes (typo fixes, comment updates) where tests add no value

## Context
The project uses SpecKit for specification-driven development, which defines *what* to build through specs, plans, and tasks. The implementation phase needed a complementary methodology to ensure the *how* is correct.

TDD naturally integrates with SpecKit: specifications define acceptance criteria, tasks break them into implementable units, and TDD ensures each unit is verified as built.

## Decision
- Adopt TDD as the implementation methodology for all `/speckit.implement` executions
- TDD applies to the implementation phase only; specification and planning phases remain unchanged
- Tests serve as verification evidence required by the constitution's "Reliability Without Compromise" principle

## Consequences
**Positive:**
- Immediate feedback on implementation correctness
- Tests serve as executable documentation of task requirements
- Forced consideration of edge cases before implementation
- Natural alignment with constitution's requirement for "verification evidence"
- Easier refactoring with confidence

**Negative:**
- Slightly longer initial implementation time per task
- Requires discipline to write tests first (not after)
- May feel redundant for very simple tasks

## References
- Constitution principle: "Reliability Without Compromise"
- Constitution requirement: "Every change set MUST include verification evidence"
- ADR-006: VM-Based Testing Framework (defines test infrastructure)
