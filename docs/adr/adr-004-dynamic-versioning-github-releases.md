# ADR-004: Dynamic Package Versioning from GitHub Releases

Status: Accepted

Date: 2025-11-15
- 2025-12-04: Added clarification for pre-release versions
- 2025-12-30: Decision: no build during GitHub Release creation because not needed with current installation method

## TL;DR
Use dynamic versioning: GitHub Release tags are the single source of truth for package version, detected automatically during build via `uv-dynamic-versioning` plugin with `hatchling` backend.

## Implementation Rules
- Do not store version in `pyproject.toml` (remove hardcoded `version` field)
- Mark version as dynamic in `pyproject.toml`: `dynamic = ["version"]`
- Use `hatchling` + `uv-dynamic-versioning` as build backend
- Configure `uv-dynamic-versioning` with `vcs = "git"` and `style = "pep440"`
- Versioning workflow: tag release on GitHub (e.g., `v1.2.0`) → GitHub Actions triggers build → `uv build` auto-injects version from tag
- Require `fetch-depth: 0` in GitHub Actions checkout step for tag access
- **Pre-release versions**: Use Semantic Versioning (SemVer) format for pre-releases:
  - Alpha releases: `v0.1.0-alpha.1`, `v0.1.0-alpha.2`
  - Beta releases: `v0.1.0-beta.1`, `v0.1.0-beta.2`
  - Release candidates: `v0.1.0-rc.1`, `v0.1.0-rc.2`
  - Final releases: `v0.1.0` (no suffix)
- **Version parsing**: Code must support SemVer pre-release identifiers (hyphen followed by dot-separated identifiers)
- **GitHub Releases**: Mark pre-release versions with "Set as a pre-release" checkbox in GitHub UI

## Context
The project uses `uv` for Python package management. While `uv`'s native build backend is optimized for static versioning, we want version to be defined solely by GitHub Releases, avoiding manual version bumps in source code. The consideration document (adr-004-versioning-uv-github.md) compares two approaches: Path A (dynamic via VCS) and Path B (static file updates via Action). Path A is cleaner for our use case since it treats the Git tag as the immutable source of truth.

For pre-release versions (alpha, beta, rc), we follow Semantic Versioning (SemVer) conventions rather than PEP 440 because:
- SemVer is more widely recognized across programming languages and ecosystems
- Format is more readable: `0.1.0-alpha.1` vs `0.1.0a1` (PEP 440)
- Python's `packaging` library normalizes SemVer to PEP 440 automatically (e.g., `0.1.0-alpha` → `0.1.0a0`)
- GitHub and most version control tools use SemVer conventions

## Decision
- Adopt dynamic versioning (Path A from adr-004-versioning-uv-github.md)
- Version is NOT stored in `pyproject.toml`
- Build system uses `hatchling` (not `uv`'s native backend) with `uv-dynamic-versioning` plugin
- No build is done during GitHub Release creation as we don't need a pre-built wheel with the current installation method
- This workflow eliminates the need to manually update `pyproject.toml` when releasing
- Use Semantic Versioning (SemVer) format for all version tags, including pre-releases
- Version parsing code must support SemVer pre-release identifiers (format: `MAJOR.MINOR.PATCH-prerelease.number`)

## Consequences
**Positive:**
- Single source of truth: Git tag is the only version reference
- No manual version bumping in `pyproject.toml`
- Clean release workflow: create Release on GitHub → Actions can build and publish automatically
- Works seamlessly with `uv` for dependency management (only build backend differs)
- SemVer pre-release format is widely recognized and readable
- Python's `packaging` library automatically normalizes SemVer to PEP 440 for compatibility

**Negative:**
- Build depends on Git tags being present; CI must use `fetch-depth: 0` to access tags
- Requires `hatchling` as build backend instead of `uv`'s native backend
- Slightly more complex `pyproject.toml` configuration
- Version parsing regex must explicitly support SemVer pre-release format

## References
- `docs/adr/considerations/adr-004-versioning-uv-github.md` - Detailed comparison of Path A vs Path B approaches
- `docs/adr/considerations/adr-004-preplexity-conversation.md` - Conversation with Grok 4.1
- [uv-dynamic-versioning plugin documentation](https://github.com/adamchainz/uv-dynamic-versioning)
- [Hatchling build backend](https://hatch.pypa.io/latest/)
