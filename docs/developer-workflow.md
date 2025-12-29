# Developer workflow

This document describes the standard contribution workflow for pc-switcher.

## Pull requests must close issues from the PR description

When a PR fixes an issue, include a closing keyword in the PR description (or title).

Examples:

- `Fixes #91`
- `Closes #123`
- `Resolves #456`

Use one line per issue.

### Why PR text (not commit messages)

In practice, relying on individual commit messages inside a PR to close issues is brittle:

- Squash merges can rewrite commit messages.
- Rebase workflows can change commit SHAs and messages.
- GitHub may treat commit references as "mentioned" without auto-closing the issue.

The most reliable convention is: the PR description is the canonical place to declare which issues should be closed when the PR is merged.

### CI enforcement

A GitHub Action enforces this rule:

- If any commit in the PR contains an issue-closing keyword (e.g. `Fixes #123`), the PR must include the same issue number with a closing keyword in the PR description/title.
- Draft PRs are exempt.

This prevents merges where issues are referenced in commits but remain open after merge.

### When there is no issue

If a PR is not associated with any issue, write `N/A` in the PR template section.

## Local checks

Before opening a PR, run the fast checks locally:

```bash
uv run basedpyright
uv run ruff check && uv run ruff format --check
uv run codespell
uv run pytest tests/unit tests/contract -v
```
