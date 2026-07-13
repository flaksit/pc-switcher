## Conflict Detection Report

Mode: new (net-new bootstrap, no existing .planning context to check against). Precedence: ADR > SPEC > PRD > DOC, with per-doc precedence integers applied (ADR=0, planning PRDs=1, system SPECs=2, speckit PRDs=3, README DOC=5).

### BLOCKERS (0)

None.

- No LOCKED-vs-LOCKED ADR contradiction: all 11 Accepted/locked ADRs (001-008, 010-012) cover distinct, complementary scopes and form a clean reference DAG (003→002, 007→006, 008→006, 010→spec-004, 012→011). No two locked decisions contradict on the same scope.
- No UNKNOWN / low-confidence classifications: all 24 docs are type-tagged with high confidence and manifest_override=true.
- No unresolved cross-ref cycle (see INFO below for the one benign navigation cycle).

### WARNINGS (0)

None.

No competing acceptance variants detected. The PRD-tier docs cover distinct features/scopes rather than redefining the same requirement with divergent acceptance:
  - docs/planning/High level requirements.md and docs/planning/Feature breakdown.md are complementary (vision vs decomposition), not contradictory.
  - specs/001-core, 002-testing-framework, 003-core-tests, 004-python-logging target different features. 003-core-tests tests 001-core (dependency, not competition).
  - "Ideas for later" in High level requirements.md are explicitly out-of-scope future ideas, not current acceptance variants.

### INFO (3)

[INFO] Auto-resolved: living Golden Copy (docs/system/, SPEC) supersedes immutable history (specs/00x, PRD) on overlapping scope
  Found: docs/system/core.md (SPEC, prec 2) overlaps specs/001-core/spec.md (PRD, prec 3); docs/system/logging.md overlaps specs/004-python-logging/spec.md; docs/system/testing.md overlaps specs/002-testing-framework/spec.md.
  Note: Per ADR-011 (Specification-Driven Development with Living Specs), docs/system/*.md is the authoritative current-state truth and specs/00x folders are frozen history. Precedence (SPEC 2 > PRD 3) agrees. No content contradiction was found between them; this records the precedence layering for transparency. Synthesized intel treats docs/system/* as authoritative for current state and specs/00x as provenance/history.

[INFO] Benign cross-ref navigation cycle: architecture.md <-> data-model.md
  Found: docs/system/architecture.md lists data-model.md in its related-docs/navigation block, and docs/system/data-model.md lists architecture.md in its "## Navigation" block (peer "see also" links among the _index-linked Golden Copy set).
  Note: This is a 2-node cycle in the literal cross_refs graph but NOT a derivation cycle. Synthesis reads each source exactly once and extracts content directly per-doc (no recursive ref traversal), so no synthesis loop or garbage output occurs. Both docs were fully synthesized. Recorded as INFO rather than a BLOCKER because gating the bootstrap on benign peer navigation links would be a false positive contrary to the intent of cycle detection.

[INFO] ADR-009 is Proposed (not locked)
  Found: docs/adr/adr-009-ai-readiness-labels.md has Status: Proposed (classification locked=false); all other ADRs are Accepted/locked.
  Note: Its decisions (AI readiness labels) are recorded but overridable by any higher-or-equal source without producing a blocker. No source in the ingest set contradicts it.
