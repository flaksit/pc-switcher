"""Static audit of the `mutates=` rule: no executor call that is not purely read-only may
reach a machine silently.

The rule is not "gate what changes content". A call may stay ungated only if it can change
NO state on the machine — no file content, no process state, no lock or other advisory
state, no package-manager database, no credential cache. Reading it as "changes content" is
what let the `flock` seizing the target's sync lock through: it writes nothing, and it
decides whether any other sync may run on that machine at all.

`mutates=` is opt-in. Nothing about a modification is structurally different from a read —
same method, same arguments, one extra keyword — so a forgotten `mutates=` produces no
error, no warning and no diff in behaviour except the one that matters: with
`--confirm-each-command` the user is never shown that operation and never asked about it,
and the debug trace does not label it as a change. That is exactly the class of omission a
review cannot catch by reading a diff, which is why it is pinned mechanically here rather
than left to `docs/dev/development-guide.md`'s prose rule.

How the audit decides read from modification: it does not. Inferring intent from a command
string ("does `install` mean apt-get install or /usr/bin/install?", "is `>` a redirect or a
comparison?") would be a guess that silently drifts. Instead every ungated call site in
`src/pcswitcher/` is enumerated below by enclosing function, and each is accounted for in
one of three tables: a pure read (`_READ_ONLY_CALLS`), a read whose incidental side effect
is deliberately tolerated (`_TOLERATED_SIDE_EFFECTS`, each with its reason stated), or a
known ungated write (`_UNGATED_WRITES`, with what it changes and the issue tracking it). A
new call that lands anywhere else fails `test_no_ungated_call_site_is_unaccounted_for`
until its author either passes `mutates=` or states, in a table, why it is left ungated.

Two known blind spots, neither of which the audit can close and both of which are safe
today: a call reached through a callable passed by reference
(`_capture_dir_digests(source_run, ...)` in `apt_sync`) is invisible because the call site
is a plain name, and only reads are routed that way today; and `declare_modification` is
not audited because `mutates` is a required argument there, so it cannot be forgotten.

The tests divide the work: the first binds the tables to the real source, so they cannot
rot into a rubber stamp; the second states the requirement the tables are measured against
— everything that is not a pure read is gated unless a stated reason or an issue says
otherwise; the third refuses a tolerated side effect that names no reason.

`TestSourceWrites` and `TestFileTransfers` below audit the same call sites from the other
side, for `PKG-FR-SOURCE-INTENT` / `PKG-FR-MANAGER-CONVERGES`: which MACHINE a gated write
reaches, and what a package job is allowed to copy between the two. Same method, same
tables-bound-to-the-source shape.

`TestTransferDestinations` and `TestPreconditionProbes` ask two further questions of those
call sites — WHERE a transfer lands (`PKG-FR-DATA-BOUNDARY`) and WHEN a precondition is
probed (`PKG-FR-SUDO-PRECONDITION`, `PKG-FR-APT-DPKG-LOCK`) — and both need the path or
command a call site actually issues, which is a variable there. `_resolve_literals` is what
recovers it: it follows f-strings, local assignments and module-level constants back to the
string literals they are built from, and hands back everything it cannot follow for the
tables to account for by name. Nothing is inferred from an expression's shape.
"""

from __future__ import annotations

import ast
from collections.abc import Mapping
from dataclasses import dataclass
from functools import cache
from pathlib import Path

import pcswitcher

_SRC = Path(pcswitcher.__file__).parent

# The executor methods that reach a machine. `declare_modification` is deliberately absent
# (see the module docstring). Matched on the method name alone, without checking the
# receiver, so renaming an executor handle cannot dodge the audit.
_GATED_METHODS = frozenset({"run_command", "start_process", "send_file", "get_file"})


@dataclass(frozen=True)
class _CallSite:
    """One `<method>` call that did NOT pass `mutates=`, and so is never gated."""

    key: str
    relpath: str
    lineno: int
    source: str


@dataclass(frozen=True)
class _UngatedWrite:
    """A modification that reaches a machine without going through the gate.

    `tracked_by` is the issue that will fix it; `None` means the omission is unaccounted
    for, which is what `test_every_ungated_write_is_tracked` refuses.
    """

    what: str
    tracked_by: str | None = None


def _collect_ungated() -> dict[str, list[_CallSite]]:
    """Every ungated executor call in `src/pcswitcher/`, grouped by call-site key.

    The key is `<relpath>::<enclosing qualname>::<method>` — deliberately not a line
    number, which every unrelated edit above would invalidate, and deliberately not the
    command text, which is usually a variable at the call site.
    """
    sites: dict[str, list[_CallSite]] = {}

    def visit(node: ast.AST, qualname: str, relpath: str) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
                visit(child, f"{qualname}.{child.name}" if qualname else child.name, relpath)
                continue
            if (
                isinstance(child, ast.Call)
                and isinstance(child.func, ast.Attribute)
                and child.func.attr in _GATED_METHODS
                and not any(keyword.arg == "mutates" for keyword in child.keywords)
            ):
                key = f"{relpath}::{qualname}::{child.func.attr}"
                sites.setdefault(key, []).append(
                    _CallSite(key=key, relpath=relpath, lineno=child.lineno, source=ast.unparse(child))
                )
            visit(child, qualname, relpath)

    for path in sorted(_SRC.rglob("*.py")):
        visit(ast.parse(path.read_text(encoding="utf-8")), "", path.relative_to(_SRC).as_posix())
    return sites


# Call sites that change nothing at all on either machine, as a count per enclosing
# function. A count rather than a per-call entry because within one function the calls are
# of one kind; the moment something that is not a pure read joins them the total stops
# matching and the audit fails. A function whose calls are of two kinds splits its count
# across this table and `_TOLERATED_SIDE_EFFECTS`, which is summed with it.
_READ_ONLY_CALLS: dict[str, int] = {
    # btrfs_snapshots: subvolume and directory listings.
    "btrfs_snapshots.py::validate_snapshots_directory::run_command": 1,
    "btrfs_snapshots.py::validate_subvolume_exists::run_command": 1,
    "btrfs_snapshots.py::list_snapshots::run_command": 1,
    # config_sync: reads the target's config and resolves its $HOME.
    "config_sync.py::_get_target_config::run_command": 1,
    "config_sync.py::_copy_config_to_target::run_command": 1,
    "disk.py::check_disk_space::run_command": 1,
    # apt_sync: `apt-get --dry-run` is a simulation, `apt-mark show*`/`apt-cache policy`/`find`/
    # `test -f`/`echo $HOME` are queries, and validate() only probes for capabilities.
    "jobs/apt_sync/commands.py::simulate_apt_transaction::run_command": 1,
    "jobs/apt_sync/commands.py::compare_deb_versions::run_command": 2,
    # The version a held package's failed install names alongside the source's
    # (`PKG-FR-APT-HOLD-VERSION`): one `apt-cache policy`, on the refusal path only.
    "jobs/apt_sync/packages.py::PackageConverger._held_version_refusal::run_command": 1,
    # `AptProbe` holds every read this job issues, so the two per-host wrappers below carry
    # all of them that go through a `run` callable: the five `/etc/apt` directory digest
    # listings, `/etc/apt/sources.list`, the two source-file reference scans (including the
    # post-write re-scan keyring collection counts against), the `cat` of a file a diff
    # implicates, `dpkg --search` over the key files, the `dpkg-query` version resolution and
    # each machine's installed-package set (`capture_source_installed`/`_target_installed`).
    "jobs/apt_sync/probe.py::AptProbe.source_run::run_command": 1,
    "jobs/apt_sync/probe.py::AptProbe.target_run::run_command": 1,
    "jobs/apt_sync/probe.py::AptProbe.source_manual_names::run_command": 1,
    "jobs/apt_sync/probe.py::AptProbe.source_policy::run_command": 1,
    "jobs/apt_sync/probe.py::AptProbe.query_target_items::run_command": 1,
    "jobs/apt_sync/probe.py::AptProbe.collect_hold_sets::run_command": 2,
    "jobs/apt_sync/probe.py::AptProbe.collect_target_policy::run_command": 1,
    "jobs/apt_sync/probe.py::AptProbe.capture_target_manual_set::run_command": 1,
    # `pro status --format json` on the target — a read, and the only thing that leaves
    # `esm_gate` is the parsed `attached` boolean (ADR-020 D-38).
    "jobs/apt_sync/probe.py::AptProbe.target_pro_attached::run_command": 1,
    # The post-`apt-get update` origin verification (ADR-020 D-35): one batched
    # `apt-cache policy` re-read of the converged target, which refuses installs but
    # changes nothing itself.
    "jobs/apt_sync/origins.py::OriginClassifier._verify::run_command": 1,
    "jobs/apt_sync/files.py::TargetFiles.backup::run_command": 1,
    "jobs/apt_sync/files.py::TargetFiles.home::run_command": 1,
    "jobs/apt_sync/job.py::AptSyncJob.validate::run_command": 5,
    "jobs/disk_space_monitor.py::DiskSpaceMonitorJob.validate::run_command": 1,
    # Demo jobs: a `seq`/`echo`/`sleep` loop used to exercise the progress UI.
    "jobs/dummy_fail.py::DummyFailJob._run_target_phase::start_process": 1,
    "jobs/dummy_success.py::DummySuccessJob._run_target_phase::start_process": 1,
    # flatpak_sync: `list`/`remotes`/`mask` listings, the per-remote keyring digest
    # (`sha256sum`, one batched read per scope), the source's ostree `repo/config` (the one
    # place a remote's `gpgkeypath` is recorded), the target's $HOME, and capability probes.
    "jobs/flatpak_sync.py::FlatpakSyncJob.capture_source_items::run_command": 1,
    "jobs/flatpak_sync.py::FlatpakSyncJob.query_target_items::run_command": 1,
    "jobs/flatpak_sync.py::FlatpakSyncJob._capture_source_remotes::run_command": 3,
    "jobs/flatpak_sync.py::FlatpakSyncJob._query_target_remotes::run_command": 2,
    "jobs/flatpak_sync.py::FlatpakSyncJob._target_home_dir::run_command": 1,
    "jobs/flatpak_sync.py::FlatpakSyncJob._capture_source_masks::run_command": 1,
    "jobs/flatpak_sync.py::FlatpakSyncJob._query_target_masks::run_command": 1,
    # Both machines' machine-level ostree trust anchors, one batched `sha256sum` each
    # (`PKG-FR-FLATPAK-REMOTE-TRUST`).
    "jobs/flatpak_sync.py::FlatpakSyncJob._capture_trust_anchors::run_command": 2,
    # Remote derivation's two source-side inputs: every installed ref including runtimes,
    # and the runtime each app is built against (one local `flatpak info` per app).
    "jobs/flatpak_sync.py::FlatpakSyncJob._capture_source_ref_origins::run_command": 1,
    "jobs/flatpak_sync.py::FlatpakSyncJob._capture_source_runtimes::run_command": 1,
    # What a filtered source remote offers under its own filter (`PKG-FR-FLATPAK-FILTER`).
    # `flatpak remote-ls` is a query: it needs no elevation even for a `--system` remote and
    # caches under the invoking user's own `~/.cache/flatpak` (measured).
    "jobs/flatpak_sync.py::FlatpakSyncJob._refs_the_remote_offers::run_command": 1,
    # The post-install origin read-back: the same `flatpak list` the capture uses, re-run on
    # the target so a ref's real provenance is checked rather than inferred (ADR-020 D-35).
    "jobs/flatpak_sync.py::FlatpakSyncJob._installed_origin_refusal::run_command": 1,
    # What the target holds once the converge loop is done, which decides whether a remote
    # the source lacks is still in use (`PKG-FR-FLATPAK-REMOTE-DELETE`).
    "jobs/flatpak_sync.py::FlatpakSyncJob._target_refs_now::run_command": 1,
    "jobs/flatpak_sync.py::FlatpakSyncJob.validate::run_command": 3,
    # folder_sync: capability probes, and the per-directory filter-file digest manifest.
    "jobs/folder_sync.py::FolderSyncJob.validate::run_command": 8,
    "jobs/folder_sync.py::FolderSyncJob._needs_copy_pass::run_command": 2,
    "jobs/install_on_target.py::InstallOnTargetJob.validate::run_command": 1,
    "jobs/install_on_target.py::InstallOnTargetJob.execute::run_command": 1,
    # manual_installs_sync: the unowned-file scan's four steps, the apt queries, and the
    # target's $HOME. Each scan step runs on whichever machine it is handed, since both are
    # read now that a finding the target already holds is not presented
    # (`PKG-FR-MANUAL-DIFF`); validate checks three tools across the two.
    "jobs/manual_installs_sync.py::ManualInstallsSyncJob._push_snippet_registry::run_command": 1,
    "jobs/manual_installs_sync.py::ManualInstallsSyncJob._scan_no_candidate_apt_packages::run_command": 1,
    "jobs/manual_installs_sync.py::ManualInstallsSyncJob._list_scan_entries::run_command": 1,
    "jobs/manual_installs_sync.py::ManualInstallsSyncJob._scan_unowned_installs::run_command": 1,
    "jobs/manual_installs_sync.py::ManualInstallsSyncJob._resolve_opt_shapes::run_command": 1,
    "jobs/manual_installs_sync.py::ManualInstallsSyncJob._directories_holding_a_file::run_command": 1,
    "jobs/manual_installs_sync.py::ManualInstallsSyncJob._installed_names::run_command": 1,
    "jobs/manual_installs_sync.py::ManualInstallsSyncJob.validate::run_command": 3,
    # The decision file and snippet registry are read with `cat`; their writes are gated.
    "jobs/packages/state.py::DecisionFile.load::run_command": 1,
    "jobs/packages/state.py::SnippetRegistry.load::run_command": 1,
    # The target's own `snap list`, read by folder_sync after the package jobs ran, deciding
    # which `~/snap/<app>/<revision>` data dirs may be mirrored (`PKG-FR-SNAP-DATA-BOUNDARY`).
    "jobs/snap_sync.py::target_snap_revisions::run_command": 1,
    "jobs/snap_sync.py::SnapSyncJob.capture_source_items::run_command": 1,
    "jobs/snap_sync.py::SnapSyncJob.query_target_items::run_command": 1,
    "jobs/snap_sync.py::SnapSyncJob.validate::run_command": 6,
    "jobs/vscode_state_sync.py::VscodeStateSyncJob.validate::run_command": 1,
    "jobs/vscode_state_sync.py::VscodeStateSyncJob.execute::run_command": 1,
    "orchestrator.py::Orchestrator._resolve_target_canonical_hostname::run_command": 1,
    "orchestrator.py::Orchestrator._check_out_of_order::run_command": 1,
}

# Modifications that reach a machine today without passing through the gate. Every entry
# is a defect: with `--confirm-each-command` the user is not shown these and is not asked.
_UNGATED_WRITES: dict[str, tuple[_UngatedWrite, ...]] = {
    "jobs/folder_sync.py::FolderSyncJob._run_rsync_pass::start_process": (
        _UngatedWrite(
            "runs the rsync pass that mirrors (and with --delete removes) the synced folders",
            tracked_by="#209",
        ),
    ),
}


def _describe(sites: list[_CallSite]) -> str:
    return "\n".join(f"    {site.relpath}:{site.lineno}  {site.source}" for site in sites)


class TestMutatesCoverage:
    def test_the_audit_sees_the_executor_call_sites(self) -> None:
        """J162 — guard against the audit silently matching nothing (moved package, renamed
        methods): a rubber-stamp audit is worse than none."""
        ungated = _collect_ungated()
        assert len(ungated) > 40, f"only {len(ungated)} ungated call sites found — the AST walk is not finding them"

    def test_no_ungated_call_site_is_unaccounted_for(self) -> None:
        """J162 — every executor call without `mutates=` is listed above as a read or a known gap.

        A newly added write that forgets `mutates=` lands in a function whose expected
        count no longer matches, and fails here until it is either gated or justified.
        """
        ungated = _collect_ungated()
        problems: list[str] = []

        for key, sites in sorted(ungated.items()):
            expected = _READ_ONLY_CALLS.get(key, 0) + len(_UNGATED_WRITES.get(key, ()))
            if len(sites) == expected:
                continue
            if expected == 0:
                problems.append(
                    f"{key}: {len(sites)} executor call(s) with no `mutates=`, not accounted for in "
                    f"tests/unit/test_mutates_audit.py.\n{_describe(sites)}\n"
                    '    Pass `mutates="<what changes>"` if this modifies the machine, '
                    "or add it to _READ_ONLY_CALLS if it does not."
                )
            else:
                problems.append(
                    f"{key}: expected {expected} ungated call(s), found {len(sites)}.\n{_describe(sites)}\n"
                    '    Pass `mutates="<what changes>"` on the new write, or update the tables.'
                )

        stale = sorted((set(_READ_ONLY_CALLS) | set(_UNGATED_WRITES)) - set(ungated))
        problems.extend(
            f"{key}: listed in the audit tables but has no ungated call — remove the entry." for key in stale
        )

        assert not problems, "`mutates=` audit failed:\n\n" + "\n\n".join(problems)

    def test_every_ungated_write_is_tracked(self) -> None:
        """J162 — the requirement: a write either carries `mutates=` or has an issue saying why not.

        Kept separate from the count check above so the two failures read differently — that one
        says "you added something unaccounted for", this one says "the codebase still has
        ungated writes". It holds today: the only ungated write left is the rsync pass under
        #209, so adding a `_UNGATED_WRITES` entry without an issue fails here.
        """
        untracked = sorted(
            f"{key}: {write.what}"
            for key, writes in _UNGATED_WRITES.items()
            for write in writes
            if write.tracked_by is None
        )
        assert not untracked, "modifications reaching a machine without the gate:\n" + "\n".join(untracked)


# ---------------------------------------------------------------------------------
# Which machine a write reaches (`PKG-FR-SOURCE-INTENT`), and what may be copied
# between them (`PKG-FR-MANAGER-CONVERGES`).
# ---------------------------------------------------------------------------------

# The write methods, plus `declare_modification` — the announcement an in-process write
# makes — because a source write that never becomes a command is still a source write.
_MACHINE_WRITES = _GATED_METHODS | {"declare_modification"}

# Receiver expressions that name the machine being synced TO. A write through one of these
# is the target's by construction, and unbounded: replicating software is what they are for.
# Anything else — a handle named for neither machine, a parameter typed `Executor`, the
# orchestrator's local executor — can reach the source and must be accounted for below.
_TARGET_HANDLES = frozenset({"self.target", "self._target", "target", "self._remote_executor"})

# The package-sync surface: the four jobs and their shared helpers. `orchestrator.py` is not
# a path here — only the one function named in `_SOURCE_WRITES` belongs to this article.
_PACKAGE_SYNC_PATHS = ("jobs/apt_sync/", "jobs/packages/")
_PACKAGE_SYNC_MODULES = frozenset({"jobs/snap_sync.py", "jobs/flatpak_sync.py", "jobs/manual_installs_sync.py"})

# `PKG-FR-SOURCE-INTENT`: the writes a sync makes on the source are exactly three, each
# required by an article of its own. The value is that article and why the write exists.
_SOURCE_WRITES: dict[str, str] = {
    # Written through whichever machine holds the item, which is the source whenever the
    # source is the one that has the software (D-08a) — hence source-capable, not target-only.
    "jobs/packages/state.py::DecisionFile.record::run_command": (
        "PKG-FR-MACHINE-SPECIFIC — the machine-specific mark, on the holding machine"
    ),
    "jobs/packages/state.py::SnippetRegistry.add::run_command": (
        "PKG-FR-MANUAL-SAME-RUN — a snippet the review authored, into the source's registry"
    ),
    # One key, two calls: the local branch is the source's half of the pause, the remote
    # branch the target's. Both are the same write, applied on both machines.
    "orchestrator.py::Orchestrator._run_snap_hold_command::run_command": (
        "PKG-FR-SNAP-REFRESH-PAUSE — the auto-refresh pause and its restore, which both machines take"
    ),
}

# Source-capable writes that are not a package sync's: they belong to other jobs and other
# requirements, and are listed only so this audit stays exhaustive over the whole package.
# A key here may never be under `_PACKAGE_SYNC_PATHS`, which is asserted.
_OUTSIDE_PACKAGE_SYNC: dict[str, str] = {
    "btrfs_snapshots.py::create_snapshot::run_command": "the pre-sync snapshot, taken on both machines",
    "btrfs_snapshots.py::validate_snapshots_directory::run_command": "creates /.snapshots on either machine",
    "btrfs_snapshots.py::cleanup_snapshots::run_command": "expires old snapshots on either machine",
    "btrfs_snapshots.py::delete_all_snapshots::run_command": "the `cleanup-snapshots` command, not a sync",
    "jobs/btrfs.py::BtrfsSnapshotJob.execute::run_command": "the snapshot job's own session folder, per machine",
    "orchestrator.py::Orchestrator._update_sync_history::declare_modification": (
        "the tool's record of this machine's role in the run — not software, and not this article's subject"
    ),
}


@dataclass(frozen=True)
class _GatedCall:
    """One `<method>` call that DID pass `mutates=`, with the handle it went through."""

    key: str
    relpath: str
    lineno: int
    receiver: str
    reaches_target_only: bool
    source: str


def _remote_executor_params(fn: ast.FunctionDef | ast.AsyncFunctionDef) -> frozenset[str]:
    """Parameters annotated `RemoteExecutor`, which is only ever the machine being synced to.

    `SnippetRegistry.replay(item_id, executor: RemoteExecutor)` is the case that needs this:
    its handle is named for neither machine, and the type is what says which one it is.
    """
    args = fn.args
    return frozenset(
        arg.arg
        for arg in (*args.posonlyargs, *args.args, *args.kwonlyargs)
        if arg.annotation is not None and "RemoteExecutor" in ast.unparse(arg.annotation)
    )


def _collect_gated(methods: frozenset[str] = _MACHINE_WRITES, *, require_mutates: bool = True) -> list[_GatedCall]:
    """Every `mutates=` call in `src/pcswitcher/`, keyed as `_collect_ungated` keys its own.

    `require_mutates=False` takes every call to `methods` instead, gated or not — which is
    what a transfer audit needs, since a `get_file` reading off a machine carries none.
    """
    calls: list[_GatedCall] = []

    def visit(node: ast.AST, qualname: str, relpath: str, target_params: frozenset[str]) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, ast.FunctionDef | ast.AsyncFunctionDef):
                name = f"{qualname}.{child.name}" if qualname else child.name
                visit(child, name, relpath, _remote_executor_params(child))
                continue
            if isinstance(child, ast.ClassDef):
                visit(child, f"{qualname}.{child.name}" if qualname else child.name, relpath, frozenset())
                continue
            if (
                isinstance(child, ast.Call)
                and isinstance(child.func, ast.Attribute)
                and child.func.attr in methods
                and (not require_mutates or any(keyword.arg == "mutates" for keyword in child.keywords))
            ):
                receiver = ast.unparse(child.func.value)
                calls.append(
                    _GatedCall(
                        key=f"{relpath}::{qualname}::{child.func.attr}",
                        relpath=relpath,
                        lineno=child.lineno,
                        receiver=receiver,
                        reaches_target_only=receiver in _TARGET_HANDLES or receiver in target_params,
                        source=ast.unparse(child),
                    )
                )
            visit(child, qualname, relpath, target_params)

    for path in sorted(_SRC.rglob("*.py")):
        visit(ast.parse(path.read_text(encoding="utf-8")), "", path.relative_to(_SRC).as_posix(), frozenset())
    return calls


def _is_package_sync(relpath: str) -> bool:
    return relpath.startswith(_PACKAGE_SYNC_PATHS) or relpath in _PACKAGE_SYNC_MODULES


class TestSourceWrites:
    """`PKG-FR-SOURCE-INTENT`: a sync changes what software the TARGET has, and makes
    exactly three writes on the source.

    The `mutates=` audit above classifies a call as read or write and stops there, so a
    fourth source write would pass it unnoticed. This one asks the other question — which
    machine does the write land on — by the handle the call goes through: `self.target` and
    a parameter typed `RemoteExecutor` are the machine being synced to, everything else can
    be the source and has to be named.

    What it cannot see: a command that reaches the source through a callable passed by
    reference, the same blind spot the module docstring records.
    """

    def test_the_audit_sees_the_gated_call_sites(self) -> None:
        """J149 — guard against the walk silently matching nothing, as above."""
        gated = _collect_gated()
        assert len(gated) > 40, f"only {len(gated)} `mutates=` call sites found — the AST walk is not finding them"

    def test_no_write_that_can_reach_the_source_is_unaccounted_for(self) -> None:
        """J149 — a new write through a handle that is not the target's lands here.

        Its author must either route it through the target, or name it — and naming a new
        one inside a package job means adding a fourth entry to `_SOURCE_WRITES`, which the
        test below refuses.
        """
        gated = _collect_gated()
        accounted = set(_SOURCE_WRITES) | set(_OUTSIDE_PACKAGE_SYNC)
        problems = [
            f"{call.key}: writes through `{call.receiver}`, which is not the target's handle, and is "
            f"listed in neither _SOURCE_WRITES nor _OUTSIDE_PACKAGE_SYNC.\n"
            f"    {call.relpath}:{call.lineno}  {call.source}"
            for call in gated
            if not call.reaches_target_only and call.key not in accounted
        ]

        seen = {call.key for call in gated if not call.reaches_target_only}
        problems.extend(
            f"{key}: listed as a source-capable write but no such call exists — remove the entry."
            for key in sorted(accounted - seen)
        )
        problems.extend(
            f"{key}: listed as outside package sync, but it is a package-sync module."
            for key in sorted(_OUTSIDE_PACKAGE_SYNC)
            if _is_package_sync(key.split("::")[0])
        )

        assert not problems, "source-write audit failed:\n\n" + "\n\n".join(problems)

    def test_a_package_sync_writes_exactly_three_things_on_the_source(self) -> None:
        """J149 — the requirement itself, spelled out here as well as in the table so that neither
        can be changed alone: the mark, the snippet, the refresh pause.
        """
        assert set(_SOURCE_WRITES) == {
            "jobs/packages/state.py::DecisionFile.record::run_command",
            "jobs/packages/state.py::SnippetRegistry.add::run_command",
            "orchestrator.py::Orchestrator._run_snap_hold_command::run_command",
        }


# What each of the three transfers a package job makes carries. Enumerated rather than
# path-matched because every destination is a variable at the call site; what this pins is
# the SURFACE — a fourth transfer, in any package job, fails the test below.
_PACKAGE_TRANSFERS: dict[str, str] = {
    "jobs/apt_sync/files.py::TargetFiles.stage_and_promote::send_file": (
        "one /etc/apt file — a repository, a pin, an apt.conf fragment or a signing key"
    ),
    "jobs/flatpak_sync.py::FlatpakSyncJob._stage_source_file::send_file": (
        "a remote's ref filter or trust anchor, staged into the target's cache"
    ),
    "jobs/manual_installs_sync.py::ManualInstallsSyncJob._push_snippet_registry::send_file": (
        "the install-snippet registry"
    ),
}


class TestFileTransfers:
    """`PKG-FR-MANAGER-CONVERGES`: software is replicated by the target's own package
    managers, so no manager's database, store or unpacked files are copied between machines.

    Stated as the transfer surface, which is what a static audit can hold: the three sites
    above are every file a package job moves, and the direction is one-way — nothing is
    fetched off the target at all. A `tar` piped through a shell command could still copy a
    store without touching `send_file`; that is out of this audit's reach and is asserted
    per manager by the jobs' own converge tests.
    """

    def test_a_package_job_transfers_only_the_three_files_it_is_allowed_to(self) -> None:
        """J150 — the three files above are every one a package job copies to the target."""
        transfers = {
            call.key: call
            for call in _collect_gated(frozenset({"send_file", "get_file"}), require_mutates=False)
            if _is_package_sync(call.relpath)
        }
        assert set(transfers) == set(_PACKAGE_TRANSFERS), "the files a package job copies changed:\n" + "\n".join(
            f"    {call.relpath}:{call.lineno}  {call.source}" for call in transfers.values()
        )

    def test_nothing_is_ever_fetched_off_the_target(self) -> None:
        """J150 — no package job reads a file off the target machine: what a manager holds there is
        queried with its own command, never copied back.
        """
        fetches = [
            f"{call.relpath}:{call.lineno}  {call.source}"
            for call in _collect_gated(frozenset({"get_file"}), require_mutates=False)
            if _is_package_sync(call.relpath)
        ]
        assert not fetches, "a package job fetched a file from the target:\n" + "\n".join(fetches)


# ---------------------------------------------------------------------------------
# Where a transfer lands (`PKG-FR-DATA-BOUNDARY`), and when a precondition is probed
# (`PKG-FR-SUDO-PRECONDITION`, `PKG-FR-APT-DPKG-LOCK`).
# ---------------------------------------------------------------------------------

# String methods that only trim or substitute: the receiver is what carries the path, so
# looking through them costs no guess. Anything else that calls is opaque by construction.
_STRING_TRANSFORMS = frozenset(
    {"removeprefix", "removesuffix", "strip", "lstrip", "rstrip", "replace", "format", "expanduser"}
)

# A resolution deeper than this is a cycle or a chain nobody reading the code could follow;
# either way the honest answer is "unresolved", not a wrong one.
_MAX_RESOLUTION_DEPTH = 32


@cache
def _module_level_constants() -> dict[str, tuple[ast.expr, ...]]:
    """Every module-level `NAME = <expr>` in `src/pcswitcher/`, by name.

    One name can be bound in two modules, so the value is a tuple: a name bound twice stays
    unresolved rather than being resolved to whichever module happened to be read last.
    Module-level is enough because the constants a path is assembled from are all there
    (`SNIPPET_REGISTRY_RELPATH`, `CONFIG_REMOTE_DIR`), and it is what makes an `import`
    followable without modelling imports at all.
    """
    bindings: dict[str, list[ast.expr]] = {}
    for path in sorted(_SRC.rglob("*.py")):
        for node in ast.parse(path.read_text(encoding="utf-8")).body:
            if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
                bindings.setdefault(node.targets[0].id, []).append(node.value)
            elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name) and node.value is not None:
                bindings.setdefault(node.target.id, []).append(node.value)
    return {name: tuple(values) for name, values in bindings.items()}


def _resolve_literals(expr: ast.expr, scope: Mapping[str, ast.expr]) -> tuple[frozenset[str], frozenset[str]]:
    """The string literals `expr` is built from, and the parts that could not be followed.

    Follows f-strings, `await`, local assignments in `scope`, module-level constants and the
    string transforms above. A parameter, an attribute or any other call is returned in the
    second set, as its source text, for the caller's table to name — which is the point: the
    audit never decides for itself that an unfollowable part is harmless.
    """
    literals: set[str] = set()
    opaque: set[str] = set()

    def walk(node: ast.expr, depth: int) -> None:
        if depth > _MAX_RESOLUTION_DEPTH:
            opaque.add(f"<unresolved past {_MAX_RESOLUTION_DEPTH} steps: {ast.unparse(node)}>")
            return
        match node:
            case ast.Constant(value=str() as text):
                literals.add(text)
            case ast.JoinedStr(values=values):
                for value in values:
                    walk(value, depth + 1)
            case ast.FormattedValue(value=inner) | ast.Await(value=inner):
                walk(inner, depth + 1)
            case ast.Name(id=name):
                if (local := scope.get(name)) is not None:
                    walk(local, depth + 1)
                elif len(bound := _module_level_constants().get(name, ())) == 1:
                    walk(bound[0], depth + 1)
                else:
                    opaque.add(name)
            case ast.Call(func=ast.Attribute(attr=attr, value=receiver)) if attr in _STRING_TRANSFORMS:
                walk(receiver, depth + 1)
            case ast.Call(func=ast.Name(id="Path" | "str"), args=[single]):
                walk(single, depth + 1)
            case _:
                opaque.add(ast.unparse(node))

    walk(expr, 0)
    return frozenset(literals), frozenset(opaque)


@dataclass(frozen=True)
class _ArgumentCallSite:
    """One executor call, kept with what it takes to resolve its arguments."""

    key: str
    relpath: str
    lineno: int
    node: ast.Call
    scope: Mapping[str, ast.expr]
    qualname: str


def _function_scope(fn: ast.FunctionDef | ast.AsyncFunctionDef) -> dict[str, ast.expr]:
    """The plain `name = <expr>` bindings in one function body.

    Order is ignored: a name assigned twice in one function would resolve to the last
    binding, which is why the audits below assert on the whole literal set rather than on a
    single value — a second binding shows up as an extra literal, not as a silent swap.
    """
    return {
        node.targets[0].id: node.value
        for node in ast.walk(fn)
        if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name)
    }


def _argument_call_sites(methods: frozenset[str]) -> list[_ArgumentCallSite]:
    """Every call to `methods` in `src/pcswitcher/`, keyed as `_collect_ungated` keys its own."""
    sites: list[_ArgumentCallSite] = []

    def visit(node: ast.AST, qualname: str, relpath: str, scope: Mapping[str, ast.expr]) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, ast.FunctionDef | ast.AsyncFunctionDef):
                name = f"{qualname}.{child.name}" if qualname else child.name
                visit(child, name, relpath, _function_scope(child))
                continue
            if isinstance(child, ast.ClassDef):
                visit(child, f"{qualname}.{child.name}" if qualname else child.name, relpath, {})
                continue
            if isinstance(child, ast.Call) and isinstance(child.func, ast.Attribute) and child.func.attr in methods:
                sites.append(
                    _ArgumentCallSite(
                        key=f"{relpath}::{qualname}::{child.func.attr}",
                        relpath=relpath,
                        lineno=child.lineno,
                        node=child,
                        scope=scope,
                        qualname=qualname,
                    )
                )
            visit(child, qualname, relpath, scope)

    for path in sorted(_SRC.rglob("*.py")):
        visit(ast.parse(path.read_text(encoding="utf-8")), "", path.relative_to(_SRC).as_posix(), {})
    return sites


def _returned_literals(symbol: str) -> tuple[frozenset[str], frozenset[str]]:
    """The literals every `return` in `<relpath>::<qualname>` resolves to, and what it could not.

    This is how a destination assembled one call away is followed: `stage_and_promote` takes
    its staging directory as a parameter, and the only place that path exists as text is the
    function that produces it.
    """
    relpath, _, qualname = symbol.partition("::")
    wanted = qualname.split(".")[-1]
    literals: set[str] = set()
    opaque: set[str] = set()
    found = False
    for node in ast.walk(ast.parse((_SRC / relpath).read_text(encoding="utf-8"))):
        if not isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef) or node.name != wanted:
            continue
        found = True
        scope = _function_scope(node)
        for statement in ast.walk(node):
            if isinstance(statement, ast.Return) and statement.value is not None:
                returned, unresolved = _resolve_literals(statement.value, scope)
                literals |= returned
                opaque |= unresolved
    assert found, f"{symbol} names no function — the destination table points at nothing."
    return frozenset(literals), frozenset(opaque)


@dataclass(frozen=True)
class _Destination:
    """Where one package transfer's remote path comes from, as the source itself says it.

    `literals` is the whole resolved set, `supplied_by` the functions whose return value is
    folded into it, and `opaque` every part that cannot be followed statically, each with
    what supplies it at run time. Stating all three is what makes a changed destination fail:
    a new path fragment lands in `literals`, and a new indirection lands in `opaque`.
    """

    argument: str
    literals: frozenset[str]
    supplied_by: tuple[str, ...]
    opaque: tuple[tuple[str, str], ...]


# The directories a package job's transfers may land in. Both staging directories sit under
# `~/.cache`, which is where `send_file` can reach at all: it is plain SFTP as the ordinary
# SSH user, so a root-owned destination is promoted afterwards by `sudo install` and is not a
# transfer. The registry is the one file that is itself the payload.
_ALLOWED_TRANSFER_ROOTS = (
    "/.cache/pc-switcher/apt-staging",
    "/.cache/pc-switcher/flatpak-staging",
    "~/.config/pc-switcher",
)

# What each manager keeps its own software in. `PKG-FR-MANAGER-CONVERGES` puts these off
# limits as a transfer destination: software arrives by the target's manager installing it.
_MANAGER_STORES = ("/var/lib/dpkg", "/var/lib/snapd", "/var/lib/flatpak", "/var/snap")

_PACKAGE_TRANSFER_DESTINATIONS: dict[str, _Destination] = {
    "jobs/apt_sync/files.py::TargetFiles.stage_and_promote::send_file": _Destination(
        argument="staged_dest",
        literals=frozenset({"/", "/.cache/pc-switcher/apt-staging"}),
        supplied_by=("jobs/apt_sync/files.py::TargetFiles.staging_dir",),
        opaque=(
            ("staging_dir", "the run's staging directory, produced by the function named in `supplied_by`"),
            ("staged_name", "the destination path flattened into one filename (`staged_name_for`)"),
            ("self.home()", "the target user's own $HOME, read once per run"),
        ),
    ),
    "jobs/flatpak_sync.py::FlatpakSyncJob._stage_source_file::send_file": _Destination(
        argument="staged",
        literals=frozenset({"/", "/.cache/pc-switcher/flatpak-staging"}),
        supplied_by=(),
        opaque=(
            ("staged_name", "a remote id flattened into one filename by the caller"),
            ("self._target_home_dir()", "the target user's own $HOME, read once per run"),
        ),
    ),
    "jobs/manual_installs_sync.py::ManualInstallsSyncJob._push_snippet_registry::send_file": _Destination(
        argument="absolute_remote_path",
        literals=frozenset({"/", "/package-snippets.yaml", "~/.config/pc-switcher"}),
        supplied_by=(),
        opaque=(("home.stdout", "the target user's own $HOME, expanded for SFTP's absolute path"),),
    ),
}

# Every `/etc/apt` location `apt_sync` owns, from the module whose whole job is to say where
# each file lives. Enumerated here so a sixth directory has to be added deliberately.
_APT_FILE_LOCATIONS = frozenset(
    {
        "/etc/apt",
        "/etc/apt/apt.conf.d",
        "/etc/apt/keyrings",
        "/etc/apt/preferences.d",
        "/etc/apt/sources.list",
        "/etc/apt/sources.list.d",
        "/etc/apt/trusted.gpg.d",
        "/usr/share/keyrings",
    }
)

# Every string the package-sync surface builds a manager-store path out of, and why. None is
# a transfer destination; each is read, and the reason says what by. Docstrings and comments
# are excluded from the scan — these modules explain at length what they do NOT write into,
# and prose that says so is the opposite of the thing being looked for.
_MANAGER_STORE_PATHS_NAMED: dict[tuple[str, str], str] = {
    ("jobs/apt_sync/job.py", "sudo fuser /var/lib/dpkg/lock-frontend"): (
        "the dpkg frontend lock, probed read-only in validate() (`PKG-FR-APT-DPKG-LOCK`)"
    ),
    ("jobs/packages/apt_policy.py", "/var/lib/dpkg/status"): (
        "the pseudo-origin `apt-cache policy` prints for a package no repository supplies — matched in "
        "apt's own output, never opened"
    ),
    ("jobs/flatpak_sync.py", "/var/lib/flatpak"): (
        "the system installation, whose `repo/<remote>.trustedkeys.gpg` is the remote signing key "
        "`_stage_source_file` reads off the SOURCE — a trust anchor, not the store's applications"
    ),
}


class TestTransferDestinations:
    """`PKG-FR-DATA-BOUNDARY`: application data belongs to `folder_sync`, so what a package
    job copies between the machines is configuration and trust material only.

    `TestFileTransfers` above pins WHICH calls copy a file; this pins WHERE each one lands,
    which is the half that decides whether a manager's store could travel. Every destination
    is a variable at the call site, so each is resolved back to the literals it is assembled
    from and compared against a table — not described in prose that the code can drift away
    from.

    What it cannot see: a `tar` piped through `run_command` copies a store without touching
    `send_file` at all. That is out of reach here and is asserted per manager by the jobs'
    own converge tests.
    """

    def test_every_package_transfer_lands_where_the_table_says(self) -> None:
        """K89 — each transfer's destination, resolved from the source rather than described.

        A destination that gains a path fragment, or a new indirection, fails here until its
        author writes down where the bytes now go.
        """
        sites = {
            site.key: site
            for site in _argument_call_sites(frozenset({"send_file", "get_file"}))
            if _is_package_sync(site.relpath)
        }
        assert set(sites) == set(_PACKAGE_TRANSFER_DESTINATIONS), (
            "a package job's set of transfers changed; describe the new one's destination here:\n"
            + "\n".join(f"    {site.relpath}:{site.lineno}  {ast.unparse(site.node)}" for site in sites.values())
        )

        problems: list[str] = []
        for key, expected in _PACKAGE_TRANSFER_DESTINATIONS.items():
            site = sites[key]
            destination = site.node.args[1]
            literals, opaque = _resolve_literals(destination, site.scope)
            for symbol in expected.supplied_by:
                supplied, also_opaque = _returned_literals(symbol)
                literals |= supplied
                opaque |= also_opaque

            if (found := ast.unparse(destination)) != expected.argument:
                problems.append(f"{key}: sends to `{found}`, the table says `{expected.argument}`")
            if literals != expected.literals:
                problems.append(
                    f"{key}: destination resolves to {sorted(literals)}, the table says {sorted(expected.literals)}"
                )
            if opaque != frozenset(name for name, _ in expected.opaque):
                problems.append(
                    f"{key}: could not follow {sorted(opaque)}, the table accounts for "
                    f"{sorted(name for name, _ in expected.opaque)} — say what supplies the new one"
                )

        assert not problems, "package transfer destinations changed:\n\n" + "\n\n".join(problems)

    def test_no_package_transfer_can_land_outside_the_staging_and_config_paths(self) -> None:
        """K89 — the requirement itself: every fragment a destination is built from is either one
        of the allowed directories or a bare filename appended to one.

        Stated against the table rather than against the source so that it says what is
        REQUIRED, not what was found; the test above is what binds the table to the source.
        """
        offenders: list[str] = []
        for key, destination in _PACKAGE_TRANSFER_DESTINATIONS.items():
            for literal in sorted(destination.literals):
                if literal.startswith(_ALLOWED_TRANSFER_ROOTS):
                    continue
                # A leading separator plus a single name is a filename appended to a
                # directory resolved above it, and names no directory of its own.
                if literal.count("/") <= 1 and literal.startswith("/"):
                    continue
                offenders.append(f"{key}: `{literal}` is under none of {list(_ALLOWED_TRANSFER_ROOTS)}")
        assert not offenders, "a package transfer's destination left the allowed paths:\n" + "\n".join(offenders)

    def test_no_package_job_names_a_manager_store_it_has_not_accounted_for(self) -> None:
        """K89 — no package job may put a manager's own store on either end of a transfer.

        Asserted one step wider than the transfers, over every string literal in the package
        jobs: a store path cannot be sent to, or read from, without first appearing as text,
        so requiring each occurrence to be named catches the read side too — which is how the
        one that does exist (flatpak's signing key, under `/var/lib/flatpak/repo`) is on the
        record instead of being assumed absent.
        """
        found: dict[tuple[str, str], list[int]] = {}
        for path in sorted(_SRC.rglob("*.py")):
            relpath = path.relative_to(_SRC).as_posix()
            if not _is_package_sync(relpath):
                continue
            tree = ast.parse(path.read_text(encoding="utf-8"))
            # A docstring is the only string that is a statement on its own.
            prose = {id(node.value) for node in ast.walk(tree) if isinstance(node, ast.Expr)}
            for node in ast.walk(tree):
                if not isinstance(node, ast.Constant) or not isinstance(node.value, str) or id(node) in prose:
                    continue
                for store in _MANAGER_STORES:
                    if store in node.value:
                        found.setdefault((relpath, node.value), []).append(node.lineno)

        problems = [
            f"{relpath}:{linenos[0]} names `{literal}`, a package manager's own store, and is not accounted "
            "for in _MANAGER_STORE_PATHS_NAMED. Nothing there may be transferred; say what reads it."
            for (relpath, literal), linenos in sorted(found.items())
            if (relpath, literal) not in _MANAGER_STORE_PATHS_NAMED
        ]
        problems.extend(
            f"{relpath}: `{literal}` is listed as a named store path but no longer appears — remove the entry."
            for relpath, literal in sorted(_MANAGER_STORE_PATHS_NAMED)
            if (relpath, literal) not in found
        )
        assert not problems, "manager-store paths in the package jobs:\n" + "\n".join(problems)

    def test_apt_moves_only_etc_apt_files_and_keyring_bytes(self) -> None:
        """K90 — `apt_sync`'s whole file surface: one transfer, into its own staging directory,
        and a file set that is `/etc/apt` plus the shared keyring directory.

        Two halves, because a transfer alone does not say what travels: the destination is
        pinned by the table above, and the CONTENT is whatever `items.py` says an apt file is
        — repository files, pins, apt.conf fragments and keyring bytes, and nothing else.
        """
        apt_transfers = {key for key in _PACKAGE_TRANSFER_DESTINATIONS if key.startswith("jobs/apt_sync/")}
        assert apt_transfers == {"jobs/apt_sync/files.py::TargetFiles.stage_and_promote::send_file"}

        locations = {
            node.value
            for node in ast.walk(ast.parse((_SRC / "jobs/apt_sync/items.py").read_text(encoding="utf-8")))
            if isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and node.value.startswith("/")
            # A bare separator names no location; it joins two halves of one.
            and node.value.strip("/")
        }
        assert locations == _APT_FILE_LOCATIONS, f"the `/etc/apt` locations apt_sync owns changed: {sorted(locations)}"
        outside = sorted(path for path in locations if not path.startswith(("/etc/apt", "/usr/share/keyrings")))
        assert not outside, f"apt_sync owns a file outside /etc/apt and the keyring directories: {outside}"

    def test_snap_sync_moves_no_file_between_the_machines(self) -> None:
        """K92 — snap's half of the boundary is a negative control: `snap_sync` has no transfer at
        all, so convergence can only be `snap install/refresh/remove` run on the target.

        There is nothing to allow-list here, and that is the assertion. The first transfer
        anyone adds to this job fails it.
        """
        transfers = [
            f"    {site.relpath}:{site.lineno}  {ast.unparse(site.node)}"
            for site in _argument_call_sites(frozenset({"send_file", "get_file"}))
            if site.relpath == "jobs/snap_sync.py"
        ]
        assert not transfers, "snap_sync copies a file between the machines:\n" + "\n".join(transfers)


# The probes that establish an environment assumption, and what each one settles. A command
# carrying one of these has to be issued while validating, where a failure is a refusal to
# start, and never while applying, where it is a half-changed machine.
_PRECONDITION_PROBES = ("sudo --non-interactive", "fuser /var/lib/dpkg/lock-frontend")

# Every `validate()` that issues one, with how many it issues. A count per body, like
# `_READ_ONLY_CALLS`: a probe that moves out of validation disappears from its count, and one
# that appears anywhere else has no entry at all.
_PRECONDITION_PROBE_SITES: dict[str, int] = {
    # Passwordless sudo on both machines, then the target's dpkg frontend lock.
    "jobs/apt_sync/job.py::AptSyncJob.validate": 3,
    # Passwordless sudo on both machines: the target installs, the source takes the
    # auto-refresh pause.
    "jobs/snap_sync.py::SnapSyncJob.validate": 2,
    # The target only, and only when a system-scope ref, remote or mask is actually in play.
    "jobs/flatpak_sync.py::FlatpakSyncJob.validate": 1,
}


class TestPreconditionProbes:
    """`PKG-FR-SUDO-PRECONDITION` / `PKG-FR-APT-DPKG-LOCK`: an environment assumption is
    settled while validating, never mid-execute.

    The difference is what a failure costs. In `validate()` a missing grant or a held lock
    refuses the whole run before anything on either machine has changed, and the message can
    carry remediation. The same discovery halfway through applying leaves a machine part
    converged, and there is nothing useful to say about it.

    So the property is structural: the commands that establish those assumptions exist only
    inside a `validate()` body. Command text is a variable at most call sites, so each one is
    resolved back to its literals rather than matched on the expression.

    What it cannot see: a probe whose command is assembled at run time from parts that never
    appear as one literal — `f"sudo {flag} true"` — would not be recognised. Nothing does that
    today, and the per-body counts below are what would notice a probe leaving validation by
    that route.
    """

    def test_the_audit_can_read_the_commands_it_scans(self) -> None:
        """K67 — guard against the resolution silently matching nothing, as elsewhere here.

        The count that matters is not how many probes exist but how many commands the
        resolver can read at all: if it stopped following f-strings it would find no probe
        anywhere and every assertion below would pass on an empty set.
        """
        readable = [
            site
            for site in _argument_call_sites(frozenset({"run_command", "start_process"}))
            if site.node.args and _resolve_literals(site.node.args[0], site.scope)[0]
        ]
        assert len(readable) > 100, f"only {len(readable)} commands resolved to any literal — the resolver is blind"

    def test_no_precondition_is_probed_outside_validate(self) -> None:
        """K67 — the requirement: sudo and the dpkg lock are established in the validation step.

        A job that re-probes either one while applying — or that probes it for the first time
        there, having chosen to degrade rather than refuse — fails here.
        """
        found: dict[str, list[str]] = {}
        for site in _argument_call_sites(frozenset({"run_command", "start_process"})):
            if not site.node.args:
                continue
            literals, _ = _resolve_literals(site.node.args[0], site.scope)
            if not any(probe in literal for literal in literals for probe in _PRECONDITION_PROBES):
                continue
            found.setdefault(f"{site.relpath}::{site.qualname}", []).append(
                f"    {site.relpath}:{site.lineno}  {ast.unparse(site.node.args[0])}"
            )

        problems = [
            f"{key}: probes a precondition outside a validate() body:\n" + "\n".join(sites)
            for key, sites in sorted(found.items())
            if not key.endswith(".validate")
        ]
        for key, expected in sorted(_PRECONDITION_PROBE_SITES.items()):
            actual = len(found.get(key, ()))
            if actual != expected:
                problems.append(f"{key}: expected {expected} precondition probe(s), found {actual}")
        problems.extend(
            f"{key}: probes a precondition but is not listed in _PRECONDITION_PROBE_SITES."
            for key in sorted(found)
            if key.endswith(".validate") and key not in _PRECONDITION_PROBE_SITES
        )

        assert not problems, "precondition probes are not confined to validate():\n\n" + "\n\n".join(problems)
