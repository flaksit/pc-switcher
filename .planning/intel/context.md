# Context (from DOCs)

Running notes from DOC-tier sources (lowest precedence). Attribution preserved; informational only — never overrides ADR/SPEC/PRD content.

## Topic: Project overview & user-facing surface (source: README.md)

- source: /home/janfr/dev/pc-switcher/README.md
- precedence: 5 (DOC, lowest)
- notes: User-facing readme. Covers what gets synced, installation, quick start, configuration, logging configuration, available CLI commands, requirements, design principles, troubleshooting, development workflow, and SpecKit commands. Self-contained per ADR-012. Points to `docs/planning/High level requirements.md`, `docs/system/architecture.md`, `docs/adr/_index.md`, `docs/dev/development-guide.md`, `CLAUDE.md`, and `src/pcswitcher/default-config.yaml`.
- usage downstream: treat as orientation/derived summary only. Any apparent claim here that conflicts with a PRD requirement or ADR/SPEC constraint is overridden by the higher-precedence source; the README should be updated to match, not used as a source of truth.
