# ADR-004: Dynamic Package Versioning from GitHub Releases

Status: Accepted
Date: 2025-11-15

## TL;DR
Use dynamic versioning: GitHub Release tags are the single source of truth for package version, detected automatically during build via `uv-dynamic-versioning` plugin with `hatchling` backend.

## Implementation Rules
- Do not store version in `pyproject.toml` (remove hardcoded `version` field)
- Mark version as dynamic in `pyproject.toml`: `dynamic = ["version"]`
- Use `hatchling` + `uv-dynamic-versioning` as build backend
- Configure `uv-dynamic-versioning` with `vcs = "git"` and `style = "pep440"`
- Versioning workflow: tag release on GitHub (e.g., `v1.2.0`) → GitHub Actions triggers build → `uv build` auto-injects version from tag
- Require `fetch-depth: 0` in GitHub Actions checkout step for tag access

## Context
The project uses `uv` for Python package management. While `uv`'s native build backend is optimized for static versioning, we want version to be defined solely by GitHub Releases, avoiding manual version bumps in source code. The consideration document (adr-004-versioning-uv-github.md) compares two approaches: Path A (dynamic via VCS) and Path B (static file updates via Action). Path A is cleaner for our use case since it treats the Git tag as the immutable source of truth.

## Decision
- Adopt Path A (dynamic versioning) from adr-004-versioning-uv-github.md
- Version is NOT stored in `pyproject.toml`
- Build system uses `hatchling` (not `uv`'s native backend) with `uv-dynamic-versioning` plugin
- GitHub Release creation triggers `uv build`, which automatically reads the tag and injects version during build
- This workflow eliminates the need to manually update `pyproject.toml` when releasing

## Consequences
**Positive:**
- Single source of truth: Git tag is the only version reference
- No manual version bumping in `pyproject.toml`
- Clean release workflow: create Release on GitHub → Actions builds and publishes automatically
- Works seamlessly with `uv` for dependency management (only build backend differs)

**Negative:**
- Build depends on Git tags being present; CI must use `fetch-depth: 0` to access tags
- Requires `hatchling` as build backend instead of `uv`'s native backend
- Slightly more complex `pyproject.toml` configuration

## References
- `docs/adr/considerations/adr-004-versioning-uv-github.md` - Detailed comparison of Path A vs Path B approaches
- [uv-dynamic-versioning plugin documentation](https://github.com/adamchainz/uv-dynamic-versioning)
- [Hatchling build backend](https://hatch.pypa.io/latest/)
