# ADR-012: Documentation Structure and Strategy

Status: Accepted
Date: 2026-01-03

Updated 2026-07-31 to fit the current state: the Folder Structure and Document Purposes below were extended with the user-facing surface that shipped after this ADR. The audience-based strategy itself is unchanged.

## TL;DR
Organize documentation by audience in subfolders, maintain CLAUDE.md and AGENTS.md as lean AI context with pointers.

## Implementation Rules

### Folder Structure
```text
docs/
├── README.md                     # Documentation index
├── jobs/                         # User-facing: what each sync job does (folder-sync, package-sync, …)
├── configuration.md              # User-facing: config-file reference
├── reading-sync-logs.md          # User-facing: how to read sync-log output
├── dev/                          # AI agent instructions
│   ├── testing-guide.md          # Expectations for writing tests
│   └── development-guide.md      # Expectations for development
├── ops/                          # Operational/setup
│   ├── testing-architecture.md   # How test infrastructure works
│   ├── testing-ops.md            # Runbooks, troubleshooting
│   └── ci-setup.md               # CI/CD configuration
├── planning/                     # Planning & scope
│   ├── high-level-requirements.md
│   └── [other planning docs]
├── premature-analysis/           # Early exploration only — not authoritative (see CLAUDE.md)
├── system/                       # Golden Copy specs (per ADR-011)
└── adr/                          # Decisions

AGENTS.md                         # Redirect to CLAUDE.md
README.md                         # User-facing, self-contained
CLAUDE.md                         # AI context, lean with pointers

tests/
└── manual-playbook.md            # Manual verification procedures
```

### Guiding Principles
1. **No duplication** - Summaries allowed if clearly marked as such; otherwise link to source of truth
2. **Audience-specific organization** - `dev/` for AI agents, `ops/` for setup/troubleshooting, `planning/` for scope
3. **Lean CLAUDE.md** - Contains the 20% needed 80% of the time; points to detailed docs on-demand
4. **Tests are tests** - Manual playbooks belong in `tests/`, not `docs/`
5. **Tool compatibility** - AGENTS.md redirects to CLAUDE.md for AI tools that look for it

### Document Purposes

| Location | Audience | Purpose | Rationale? |
| -------- | -------- | ------- | ---------- |
| `docs/README.md` | All | Documentation index | No |
| `docs/adr/` | All | Architectural decisions | Decision-level, 1-2 lines |
| `docs/adr/considerations/` | All | Background, evidence, design rationale | Yes — its home |
| `docs/jobs/` | Users | What each sync job does and shows | Consequences only |
| `docs/configuration.md` | Users | Config-file reference | No |
| `docs/reading-sync-logs.md` | Users | How to read sync-log output | No |
| `docs/premature-analysis/` | — | Early exploration only; not authoritative | — |
| `docs/dev/` | AI agents | Instructions and expectations | No |
| `docs/ops/` | Developers, DevOps | Setup, architecture, troubleshooting | No |
| `docs/planning/` | Project owner | Scope and intent | No |
| `docs/system/` | PO, architects, devs | Golden Copy specs (per ADR-011) | No |
| `AGENTS.md` | AI agents | Redirect to CLAUDE.md | No |
| `CLAUDE.md` | AI agents | Essential context with pointers | No |
| `README.md` | Users, new contributors | Self-contained quick start | No |
| `tests/manual-playbook.md` | Project owner | Manual verification for releases | No |

### Altitudes and the rationale rule

Every doc sits at one altitude. Content at the wrong altitude is moved, not trimmed. `docs/system/` wins over any other doc it disagrees with.

- **Intent** → `docs/planning/` — what a capability is for and will not do.
- **Specification** → `docs/system/` — one line per article, semantic IDs, `Lineage:` per ADR-011, `Impl:` where the implementer is not obvious. No justification prose.
- **Behaviour** → `docs/jobs/` — what a user turns on and gets asked. Tables over prose.
- **Decision** → `docs/adr/` — choice + rejected alternative, a line or two. Detailed design goes to `docs/system/`, not here.
- **Rationale, evidence, measurements** → `docs/adr/considerations/`.

Per sentence: does it change what the reader does or decides? Yes — keep it. No — move it to `docs/adr/considerations/`, or delete it if it just restates the article. A per-topic rationale file keyed by article ID beats `Why:` lines on every article — the latter is what makes docs unreadable.

No prose paragraph in `docs/` may exceed 40 words. One idea per paragraph.

### CLAUDE.md and AGENTS.md Strategy
- Brief summary of sync scope with link to `docs/planning/high-level-requirements.md`
- Pointers to `docs/dev/` for agent instructions
- Essential commands, constraints, and patterns inline
- Avoid duplicating content that agents can load on-demand

AGENTS.md as a simple redirect to CLAUDE.md for compatibility with various AI tools.

## Context
Documentation spread across many files with unclear navigation, significant overlap (especially in testing docs), and uncertainty about appropriate documentation level for a solo-developer project. AI agents are primary consumers of development/testing docs.

## Decision
- Organize docs by audience in subfolders (`dev/`, `ops/`, `planning/`)
- Keep CLAUDE.md lean with pointers to detailed docs
- Maintain AGENTS.md as redirect for cross-tool compatibility
- Treat manual playbooks as test artifacts, not documentation
- Apply DRY principle: no duplication except clearly-marked summaries

## Consequences

**Positive:**
- Clear navigation: know where to look based on what you need
- Reduced maintenance: less duplication means less drift
- AI-optimized: agents load only relevant docs per task
- Audience-appropriate: different detail levels for different needs

**Negative:**
- CLAUDE.md pointers require discipline to keep accurate

## References
- GitHub Issue #134: Clean up and restructure docs
- ADR-011: Hybrid Specification-Driven Development with Living Specs