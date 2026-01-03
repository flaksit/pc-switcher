# System Documentation

This folder contains the "Golden Copy" of the system specification. It represents the current state of the system, consolidated from various feature implementations.

## Core Documentation

- [Architecture](architecture.md): High-level design, components, and interactions.
- [Data Model](data-model.md): Core entities, schemas, and data flows.

## Domain Specifications

- [Foundation](foundation.md): Core infrastructure, CLI, and orchestration.
- [Testing Framework](testing.md): Testing framework, infrastructure, and strategy.
- [Logging](logging.md): Logging infrastructure and configuration.

## Workflow

This documentation is the **Living Truth**.
- **For small changes:** Update these files directly.
- **For big features:** Use SpecKit in `specs/` folders, then consolidate the results here upon completion.
