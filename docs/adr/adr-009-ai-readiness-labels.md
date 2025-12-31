# ADR-009: AI Readiness Labels for Issue Triage

**Status**: Proposed
**Date**: 2025-12-31

## TL;DR

Use three GitHub labels to classify issues by AI readiness: `ai:ready` (can be implemented directly by AI), `ai:needs-speckit` (too large, needs speckit workflow first), and `ai:unclear` (needs human clarification before AI can work on it).

## Implementation Rules

1. Every open issue SHOULD have exactly one `ai:*` label
2. Triage flow:
   - If scope/expected outcome is unclear → `ai:unclear`
   - If clear but too large/complex for single-pass implementation → `ai:needs-speckit`
   - If clear and small enough for direct implementation → `ai:ready`
3. `ai:ready` does not mean zero human interaction - AI may ask clarifying questions during implementation
4. `ai:unclear` is a temporary state - once clarified, re-triage to `ai:ready` or `ai:needs-speckit`
5. These labels are orthogonal to priority labels (`P*`)

## Context

With AI agents capable of implementing GitHub issues, we need a way to quickly identify which issues can be delegated to AI without significant human preparation. This enables:

- Efficient task delegation to AI agents
- Clear identification of issues needing human input first
- Distinction between "needs clarification" and "needs breakdown via speckit"

## Decision

Introduce three mutually exclusive labels:

| Label | Meaning |
| ----- | ------- |
| `ai:ready` | Clear enough + small enough → give to AI directly |
| `ai:needs-speckit` | Clear enough to know it's big → needs speckit workflow |
| `ai:unclear` | Can't determine scope or expected outcome → human clarifies first |

### Decision Tree

```text
Can we understand what's expected from the issue?
├─ NO  → ai:unclear (human clarifies, then re-triage)
└─ YES → Is it small enough for direct implementation?
         ├─ YES → ai:ready
         └─ NO  → ai:needs-speckit
```

### What "small enough" means

An issue is small enough for `ai:ready` if an AI agent can reasonably:
- Understand the full scope from the issue description
- Implement it in a single working session
- Not require architectural decisions or major refactoring

## Consequences

### Positive

- Quick visual identification of AI-delegatable work
- Clear workflow for issues that need human attention first
- Prevents AI from struggling with under-specified or oversized issues

### Negative

- Additional triage overhead (but minimal - one label per issue)
- Judgment call on "small enough" boundary
- Labels may become stale if issues evolve

## References

- [SpecKit workflow](../../specs/) - Process for breaking down large issues
