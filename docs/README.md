# Documentation Index

This directory contains all project documentation, organized by audience.

## For Users

- [configuration.md](configuration.md) - Complete reference for `~/.config/pc-switcher/config.yaml` (all options, defaults, config keys)
- [reading-sync-logs.md](reading-sync-logs.md) - Interpreting per-file rsync itemize codes (`<f+++++++++`, `cd+++++++++`, …) in FULL-level logs

## Job behaviour (`jobs/`)

What each sync job does, kept separate from its configuration:

- [package-sync.md](jobs/package-sync.md) - The four package jobs (`apt_sync`, `snap_sync`, `flatpak_sync`, `manual_installs_sync`): item -> diff -> review -> converge, per-manager review, machine-specific packages, install snippets
- [folder-sync.md](jobs/folder-sync.md) - `folder_sync` filter-rule semantics, `authorized_keys` guidance, and always-excluded paths
- [vscode-state-sync.md](jobs/vscode-state-sync.md) - `vscode_state_sync` selective, SQLite-aware merge that preserves machine-bound secrets

## For AI Agents (`dev/`)

Instructions and expectations for AI agents when developing:

- [development-guide.md](dev/development-guide.md) - Development workflow and code expectations
- [testing-guide.md](dev/testing-guide.md) - How to write tests

## For Operations (`ops/`)

Setup, architecture understanding, and troubleshooting:

- [testing-architecture.md](ops/testing-architecture.md) - How the test infrastructure works
- [testing-ops.md](ops/testing-ops.md) - Runbooks, VM management, troubleshooting
- [ci-setup.md](ops/ci-setup.md) - CI/CD configuration and secrets

## Project Scope (`planning/`)

Requirements and planning artifacts:

- [High level requirements.md](planning/High%20level%20requirements.md) - Project vision, scope, and constraints
- [Feature breakdown.md](planning/Feature%20breakdown.md) - Feature planning
- [Issue triage 2025-12-31.md](planning/Issue%20triage%202025-12-31.md) - Issue analysis

## Specifications (`system/`)

Living specifications per [ADR-011](adr/adr-011-sdd-with-living-specs.md):

- [architecture.md](system/architecture.md) - System architecture
- [data-model.md](system/data-model.md) - Core entities and schemas
- [core.md](system/core.md) - Core infrastructure spec
- [logging.md](system/logging.md) - Logging specification
- [testing.md](system/testing.md) - Testing specification

## Decisions (`adr/`)

Architectural Decision Records:

- [_index.md](adr/_index.md) - Summary of all ADRs

## Other

- [Premature analysis/](Premature%20analysis/) - Early exploration (historical, do not use as requirements)
