# ADR-001: Use Architecture Decision Records (ADR)

Status: Accepted  
Date: 2025-11-13

## TL;DR
Use immutable Architecture Decision Records (ADRs) to document key architectural decisions.

## Implementation Rules
- Follow the folder structure below.
- Keep `_index.md` up to date as a list with all and only current ADRs.
- Each ADR must follow the structure below.
- Avoid duplicating information across ADR sections; only repeat in TL;DR. Repeat in other sections only if necessary for clarity.
- Keep ADRs concise. Place background and considerations in `docs/adr/considerations/` documents.
- ADRs are immutable: they can be superseded but not edited after acceptance.
- Add two-way references between superseded and current ADRs.

### Folder structure

```plain
docs/adr/
├── considerations/                      -- Documents with additional context and considerations for ADRs
├── _index.md                            -- Summary of all current ADRs
├── adr-001-adr.md                       -- This ADR
├── adr-002-postgresql-timescaledb.md    -- ADR-002
└── ...
```

### Document structure

```markdown
# ADR-XXX: [Title]

Status: [Draft|Accepted|Superseded by ADR-XXX|Deprecated]
Date: YYYY-MM-DD
Supersedes: ADR-XXX (if applicable)

## TL;DR
One sentence decision summary for quick AI parsing.

## Implementation Rules
- Specific technical constraints
- Required patterns/approaches
- Forbidden approaches

## Context
[Brief context - keep under 200 words]

## Decision
[What was decided - bullet points preferred]

## Consequences
**Positive**: Critical implementation requirements (bullet points)
**Negative**: Known anti-patterns from this decision (bullet points)

## References
- Links to relevant discussions, chats, documents or websites
```

## Context
ADRs (Architecture Decision Records) are lightweight documents that capture important architectural decisions made during a project's development.

## Decision
- Use ADRs to document key architectural decisions
- See above [implementation rules](#implementation-rules)
- In the main project repository, or in subrepos if only relevant for that context

Example ADR: see [`002-postgresql-timescaledb.md`](docs/adr/002-postgresql-timescaledb.md).

### AI agent integration strategy
- AI agent reads `_index.md` first (always in context)
- Loads specific ADRs only when relevant to current task
- Use `conversation_search` to find relevant ADRs

TODO Add this to the AI agent's context loading logic like AGENTS.md or CLAUDE.md.

## Granularity Guidelines
These guidelines help keep ADR maintenance overhead low while preserving meaningful decision boundaries for a solo project.

Combine when decisions are:
- Tightly coupled – changing one necessitates changing the other
- Same abstraction level – e.g. both infrastructure OR both application-level
- Same timeline – decided together and likely to change together

Split when decisions:
- Can evolve independently – different tools, lifecycles, or replacement likelihood
- Have different stakeholders (even if it is just different "hats" you wear)
- Cross major boundaries – infrastructure vs. application vs. business/domain logic

### Anti-pattern: Over-Granularity
Avoid creating many tiny ADRs that add maintenance cost without clarity benefits. For example, DO NOT split like:
- ADR-004: Use Kubernetes
- ADR-005: Use K3s instead of K8s
- ADR-006: Use Hetzner Cloud
- ADR-007: Use Terraform
- ADR-008: Use kube-hetzner module

Prefer a higher-level ADR that captures the cohesive deployment/infrastructure decision, unless individual elements are likely to change independently.

## Consequences
**Positive:**
- Future self documentation - Remember why you chose PostgreSQL over MongoDB 6 months later
- Decision traceability - Understand the reasoning behind technology choices
- Avoid repeated debates - Don't re-argue the same decisions
- Rapid context switching and mistake learning
- Onboarding aid - If you ever bring someone else onto the project

Specific benefits of immutability:
- Time travel - Understand why past-you made certain choices
- Context preservation - Original constraints and reasoning remain clear
- Rapid context switching - Quickly recall the "why" behind complex choices
- Mistake learning - Track which decisions worked out and which didn't

**Negative:**
- Initial overhead of writing ADRs
- Requires discipline to keep them updated
