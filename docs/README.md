# Documentation Index

This directory contains all project documentation, organized by audience.

## For Users

- [configuration.md](configuration.md) - Complete reference for `~/.config/pc-switcher/config.yaml` (all options, defaults, config keys)
- [reading-sync-logs.md](reading-sync-logs.md) - Interpreting per-file rsync itemize codes (`<f+++++++++`, `cd+++++++++`, …) in FULL-level logs

## Job behaviour (`jobs/`)

What each sync job does, kept separate from its configuration:

- [package-sync.md](jobs/package-sync.md) - The six package jobs (`apt_sync`, `snap_sync`, `flatpak_sync`, `manual_deb_sync`, `manual_snap_sync`, `manual_installs_sync`): item -> diff -> review -> converge, per-manager review, machine-specific packages, install snippets
- [folder-sync.md](jobs/folder-sync.md) - `folder_sync` filter-rule semantics, `authorized_keys` guidance, and always-excluded paths
- [vscode-state-sync.md](jobs/vscode-state-sync.md) - `vscode_state_sync` selective, SQLite-aware merge that preserves machine-bound secrets

## For AI Agents (`dev/`)

Instructions and expectations for AI agents when developing:

- [development-guide.md](dev/development-guide.md) - Development workflow and code expectations
- [testing-guide.md](dev/testing-guide.md) - How to write tests
- [package-sync-scenario-coverage.md](dev/package-sync-scenario-coverage.md) - Every branch the package sync requirements impose, the test that proves each, and what is still unproven

## For Operations (`ops/`)

Setup, architecture understanding, and troubleshooting:

- [testing-architecture.md](ops/testing-architecture.md) - How the test infrastructure works
- [testing-ops.md](ops/testing-ops.md) - Runbooks, VM management, troubleshooting
- [ci-setup.md](ops/ci-setup.md) - CI/CD configuration and secrets

## Project Scope (`planning/`)

Requirements and planning artifacts:

- [high-level-requirements.md](planning/high-level-requirements.md) - Project vision, scope, and constraints
- [package-sync-user-requirements.md](planning/package-sync-user-requirements.md) - What package sync is for and how it behaves, in prose. Authoritative for intent; read this first
- [package-sync-conformance-criteria.md](planning/package-sync-conformance-criteria.md) - The same intent as individually checkable articles (`PKG-FR-*` obligations, `PKG-NG-*` non-goals), for verifying an implementation against
- [feature-breakdown.md](planning/feature-breakdown.md) - Feature planning
- [issue-triage-2025-12-31.md](planning/issue-triage-2025-12-31.md) - Issue analysis

## Specifications (`system/`)

Living specifications per [ADR-011](adr/adr-011-sdd-with-living-specs.md):

- [architecture.md](system/architecture.md) - System architecture
- [data-model.md](system/data-model.md) - Core entities and schemas
- [core.md](system/core.md) - Core infrastructure spec
- [package-sync.md](system/package-sync.md) - Package sync specification
- [logging.md](system/logging.md) - Logging specification
- [testing.md](system/testing.md) - Testing specification

## Decisions (`adr/`)

Architectural Decision Records:

- [_index.md](adr/_index.md) - Summary of all ADRs

## Other

- [premature-analysis/](premature-analysis/) - Early exploration (historical, do not use as requirements)
