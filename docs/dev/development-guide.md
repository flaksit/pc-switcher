# Development Guide for AI Agents

This guide provides instructions and expectations for AI agents when developing code for pc-switcher.

## Before You Start

**Always read first:**
- `~/.claude/CLAUDE.md` - General agent instructions
- `~/.claude/python_conventions.md` and `~/.claude/python_tools.md` - Python coding conventions
- `docs/adr/_index.md` - Architectural decisions

**For feature work:**
- Read the relevant spec in `docs/system/` or `specs/`
- Check existing patterns in the codebase

## Development Commands

```bash
# Type checking
uv run basedpyright

# Linting and formatting
uv run ruff check . && uv run ruff format .

# Spell checking
uv run codespell

# Unit and contract tests
uv run pytest tests/unit tests/contract -v

# Integration tests (requires VM infrastructure)
tests/run-integration-tests.sh
```

**Critical:** Always use `uv run` for Python commands. Never use `python3`, `python`, or `pip` directly.

## Pull Request Workflow

### PRs Must Close Issues

When a PR fixes an issue, include a closing keyword in the PR description (not just commit messages):

```
Fixes #91
Closes #123
Resolves #456
```

Use one line per issue.

**Why PR text, not commit messages:** Squash merges and rebases can rewrite commit messages. The PR description is the reliable place for issue references.

**When there is no issue:** Write `N/A` in the PR template section.

### CI Enforcement

A GitHub Action enforces this rule:
- If any commit contains an issue-closing keyword, the PR must include the same issue number in the description
- Draft PRs are exempt

### Draft PRs for Integration Tests

**Always create PRs as draft** initially. Integration tests only run on ready (non-draft) PRs to save CI resources.

## Code Expectations

### Follow Existing Patterns

- Check how similar functionality is implemented elsewhere in the codebase
- Use the same naming conventions, error handling patterns, and code organization
- When in doubt, look at recent commits for style guidance

### Reliability First

Per project principles, reliability is the top priority:
1. No data loss
2. Conflict detection
3. Consistent state after sync

This means:
- Validate inputs thoroughly
- Handle errors explicitly
- Use btrfs snapshots for safety
- Test edge cases

### Keep It Simple

Avoid over-engineering:
- Don't add features beyond what's requested
- Don't create abstractions for one-time operations
- Don't add comments stating the obvious
- Don't add error handling for impossible scenarios

### Type Everything

- All functions must have type annotations
- Use `basedpyright` for type checking
- Prefer explicit types over `Any`

## Documentation Updates

When you make changes:
- Update docstrings for modified functions
- Update relevant docs if behavior changes
- Update `docs/system/` specs if requirements change (add lineage per ADR-011)
- **Never update `specs/` folder** - those are immutable history

## Testing Expectations

See [Testing Guide](testing-guide.md) for detailed testing instructions.

Quick summary:
- Write unit tests for new logic
- Write integration tests for SSH/system operations
- Follow spec-driven naming when applicable

## Commit Messages

Follow conventional commit style:
- `fix:` for bug fixes
- `feat:` for new features
- `refactor:` for code restructuring
- `docs:` for documentation
- `test:` for test changes

Keep messages concise and focused on the "why".

## References

- [Testing Guide](testing-guide.md) - How to write tests
- [docs/ops/ci-setup.md](../ops/ci-setup.md) - CI configuration
- [docs/adr/](../adr/) - Architectural decisions
- [docs/system/](../system/) - Living specifications
