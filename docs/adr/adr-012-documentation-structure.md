# ADR-012: Documentation Structure and Strategy

Status: Accepted
Date: 2026-01-03

## TL;DR
Organize documentation by audience in subfolders, maintain CLAUDE.md and AGENTS.md as lean AI context with pointers.

## Implementation Rules

### Folder Structure
```text
docs/
├── dev/                          # AI agent instructions
│   ├── testing-guide.md          # Expectations for writing tests
│   └── development-guide.md      # Expectations for development
├── ops/                          # Operational/setup
│   ├── testing-architecture.md   # How test infrastructure works
│   ├── testing-ops.md            # Runbooks, troubleshooting
│   └── ci-setup.md               # CI/CD configuration
├── planning/                     # Planning & scope
│   ├── High level requirements.md
│   └── [other planning docs]
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

| Location | Audience | Purpose |
| -------- | -------- | ------- |
| `docs/adr/` | All | Architectural decisions |
| `docs/dev/` | AI agents | Instructions and expectations for code/test writing |
| `docs/ops/` | Developers, DevOps | Setup, architecture understanding, troubleshooting |
| `docs/planning/` | Project owner | Scope, requirements, planning artifacts |
| `docs/system/` | AI agents, developers | Golden Copy specs (per ADR-011) |
| `AGENTS.md` | AI agents | Redirect to CLAUDE.md for compatibility |
| `CLAUDE.md` | AI agents | Essential context with pointers to details |
| `README.md` | Users, new contributors | Self-contained quick start |
| `tests/manual-playbook.md` | Project owner | Manual verification procedures for releases |

### CLAUDE.md and AGENTS.md Strategy
- Brief summary of sync scope with link to `docs/planning/High level requirements.md`
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