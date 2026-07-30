# System Documentation

The "Golden Copy" of the system specification: what pc-switcher does **today**. Every claim here must match `src/pcswitcher/`. Where this folder and `specs/` disagree, this folder wins — `specs/` holds the planning material a feature was built from, not the shipped behaviour.

## Core Documentation

- [Architecture](architecture.md): components, their interactions, and the sync lifecycle.
- [Data Model](data-model.md): core entities, schemas, and data flows.

## Domain Specifications

- [Core](core.md): CLI, orchestration, locking, snapshots, configuration, and the job contract.
- [Package Sync](package-sync.md): the four package jobs, their shared contract and their plan/review/apply pipeline.
- [Testing Framework](testing.md): testing framework, infrastructure, and strategy.
- [Logging](logging.md): logging infrastructure and configuration.

## Workflow

- **Small changes:** update these files with the code, in the same commit.
- **Big features:** use SpecKit in `specs/`, then consolidate the result here on completion (ADR-011).
