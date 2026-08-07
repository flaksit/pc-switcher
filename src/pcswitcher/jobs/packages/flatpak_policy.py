"""Which installed flatpak refs no remote can reproduce, shared by `flatpak_sync` and
`manual_flatpak_sync`.

The two jobs ask the same question from opposite ends — `flatpak_sync` asks which refs to
DROP from both its manifests, `manual_flatpak_sync` asks which refs to PICK UP — so the
answer has to be one definition rather than two that happen to agree. A predicate that
drifts here does not merely misfile an item: a ref `flatpak_sync` excludes and this module
does not flag is replicated by nobody and reported nowhere.

Shared here rather than duplicated per job, and here rather than on `PackageSyncJob`, for
the reason `packages/apt_policy.py` records: `PKG-FR-JOB-INDEPENDENCE` forbids one manager's diff on the shared
base class, this module defines no class and sits in no job's MRO, and
`manual_flatpak_sync` never imports `flatpak_sync` (`PKG-FR-MANUAL-SCOPE`) because both import a third
module instead.

## The predicate, and how it was established

A ref is unreproducible exactly when its `origin` names no remote `flatpak remotes` lists
for its own installation scope. That one test covers both shapes issue #252 names, which
is why there is one predicate and not two.

Measured on Flatpak 1.14.6 (Ubuntu 24.04) in a throwaway installation
(`FLATPAK_USER_DIR`), and cross-read against the flatpak 1.14.6 sources, so nothing below
is inference:

- **Installed from a local bundle.** `flatpak install --bundle` writes a pseudo-remote into
  the installation's `repo/config` — `create_origin_remote_config`, `common/flatpak-dir.c`
  — named `<last dot-component of the application id, lowercased>-origin` (measured:
  `org.pcswtest.Bundle` -> `bundle-origin`), with `xa.noenumerate=true`, `gpg-verify=false`
  and `url=` taken from the bundle's `origin` metadata field. `flatpak build-bundle` writes
  that field ONLY when given `--repo-url`, so an ordinary bundle's origin remote carries an
  EMPTY url. `flatpak list --columns=origin` prints the pseudo-remote's name;
  `flatpak remotes` prints nothing, and only `--show-disabled` reveals it, as
  `disabled,no-enumerate,no-gpg-verify`.
- **Installed from a remote since deleted.** After `flatpak remote-delete --force
  localrepo`, the ref's `origin` column still prints `localrepo` — `flatpak list` reads the
  origin off the deploy data, which `flatpak_dir_remove_remote` never touches — while
  `repo/config` holds no `[remote "localrepo"]` section and both `flatpak remotes` and
  `flatpak remotes --show-disabled` print nothing. The origin is a dangling name.

What `flatpak remotes` actually hides is DISABLED remotes, not no-enumerate ones
(`app/flatpak-builtins-remote-list.c`: the sole filter is
`disabled && !opt_show_disabled`), and `flatpak_dir_get_remote_disabled` counts an EMPTY
url as disabled. That is why a bundle origin drops out: not because it is no-enumerate but
because it has nowhere to fetch from. The two facts land on the same answer here, and the
url is the one that matters — a remote with no url cannot be provisioned on the other
machine, so a ref depending on it cannot be installed there.

The corollary is deliberate rather than a gap: a bundle built WITH `--repo-url` produces an
origin carrying a real URL, `flatpak remotes` lists it, and this predicate calls the ref
REPRODUCIBLE — correctly, because that URL is a repository `flatpak_sync` can add on the
target and install the ref from. Only refs with no reachable source of bytes are carved
out.

That also makes `flatpak remotes` without `--show-disabled` the right authority for a
second reason: it is exactly the set `flatpak_sync` already captures and provisions
(`_FLATPAK_REMOTES_CMD_TEMPLATE`). Widening to `--show-disabled` would put url-less bundle
origins back into `flatpak_sync`'s remote derivation, which is the bug being fixed.

Scope is part of the question, not context around it: flatpak tracks remotes per
installation, so `flathub` configured system-wide says nothing about a user-scope ref whose
origin is `flathub` (`PKG-FR-APT-ORIGIN-DERIVED`). The lookup is therefore always keyed on `(scope, origin)`.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Literal, Protocol

__all__ = [
    "FLATPAK_REMOTE_NAMES_CMD",
    "SCOPES",
    "ScopedOrigin",
    "partition_unreproducible",
    "remote_names",
    "scope_flag",
]

#: The two installation scopes this project's item model represents (`PKG-FR-APT-ORIGIN-DERIVED`). Flatpak permits
#: further named installations; a machine using one would need its own modelling decision,
#: not a silent default (`flatpak_sync._parse_flatpak_list`).
SCOPES: tuple[Literal["user", "system"], ...] = ("user", "system")


def scope_flag(scope: str) -> str:
    """The `flatpak` CLI flag selecting one installation scope."""
    return "--user" if scope == "user" else "--system"


#: Names alone, because the only thing the reproducibility question asks of a remote is
#: whether it is configured at all — `flatpak_sync` reads the same command with the four
#: columns its own remote replication needs. Deliberately WITHOUT `--show-disabled`: see
#: the module docstring for why the enumerable set is the authority.
FLATPAK_REMOTE_NAMES_CMD = "flatpak remotes {flag} --columns=name"


def remote_names(output: str) -> frozenset[str]:
    """The remote names one scope's `FLATPAK_REMOTE_NAMES_CMD` printed, one per line."""
    return frozenset(line.strip() for line in output.splitlines() if line.strip())


class ScopedOrigin(Protocol):
    """What the predicate needs of an installed ref: its scope and the remote its `origin`
    column names. A structural type rather than an import, so this module stays free of
    `flatpak_sync`'s item dataclasses and both jobs can pass their own.
    """

    @property
    def scope(self) -> str: ...

    @property
    def origin(self) -> str: ...


def partition_unreproducible[T: ScopedOrigin](
    items: Sequence[T], configured: Mapping[str, frozenset[str]]
) -> tuple[list[T], list[T]]:
    """Split installed refs into `(reproducible, unreproducible)`, preserving order.

    Unreproducible means the ref's `origin` names no remote configured in its OWN scope
    (module docstring). `configured` is `{scope: remote names}` for the machine the refs
    came from; a scope missing from it has no configured remote, so every ref in it is
    unreproducible — which is the honest reading, since a `flatpak remotes` that answered
    nothing is guarded at the call site (`require_answer`) rather than silently treated as
    "everything is fine".
    """
    reproducible = [item for item in items if item.origin in configured.get(item.scope, frozenset())]
    unreproducible = [item for item in items if item.origin not in configured.get(item.scope, frozenset())]
    return reproducible, unreproducible
