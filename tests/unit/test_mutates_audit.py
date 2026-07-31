"""Static audit of the `mutates=` rule: no executor call may CHANGE a machine silently.

`mutates=` is opt-in. Nothing about a write is structurally different from a read — same
method, same arguments, one extra keyword — so a forgotten `mutates=` produces no error,
no warning and no diff in behaviour except the one that matters: with
`--confirm-each-command` the user is never shown that modification and never asked about
it, and the debug trace does not label it as a change. That is exactly the class of
omission a review cannot catch by reading a diff, which is why it is pinned mechanically
here rather than left to `docs/dev/development-guide.md`'s prose rule.

How the audit decides read from write: it does not. Inferring intent from a command string
("does `install` mean apt-get install or /usr/bin/install?", "is `>` a redirect or a
comparison?") would be a guess that silently drifts. Instead every ungated call site in
`src/pcswitcher/` is enumerated below by enclosing function, and each is accounted for as
either a deliberate read (`_READ_ONLY_CALLS`) or a known ungated write (`_UNGATED_WRITES`,
with what it changes and the issue tracking it). A new call that lands anywhere else fails
`test_no_ungated_call_site_is_unaccounted_for` until its author either passes `mutates=` or
states, in the table, why it changes nothing.

Two known blind spots, neither of which the audit can close and both of which are safe
today: a call reached through a callable passed by reference
(`_capture_dir_digests(source_run, ...)` in `apt_sync`) is invisible because the call site
is a plain name, and only reads are routed that way today; and `declare_modification` is
not audited because `mutates` is a required argument there, so it cannot be forgotten.

The two tests divide the work: the first binds the tables to the real source, so they
cannot rot into a rubber stamp; the second states the requirement the tables are measured
against — every write is gated unless an issue says otherwise.

`TestSourceWrites` and `TestFileTransfers` below audit the same call sites from the other
side, for `PKG-FR-SOURCE-INTENT` / `PKG-FR-MANAGER-CONVERGES`: which MACHINE a gated write
reaches, and what a package job is allowed to copy between the two. Same method, same
tables-bound-to-the-source shape.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass
from pathlib import Path

import pcswitcher

_SRC = Path(pcswitcher.__file__).parent

# The executor methods that reach a machine. `declare_modification` is deliberately absent
# (see the module docstring). Matched on the method name alone, without checking the
# receiver, so renaming an executor handle cannot dodge the audit.
_GATED_METHODS = frozenset({"run_command", "start_process", "send_file", "get_file"})


@dataclass(frozen=True)
class _CallSite:
    """One `<method>` call that did NOT pass `mutates=`."""

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


# Call sites that change nothing on either machine, as a count per enclosing function.
# A count rather than a per-call entry because within one function the calls are of one
# kind; the moment a write joins them the total stops matching and the audit fails.
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
    # implicates, `dpkg --search` over the key files, and the `dpkg-query` version resolution.
    "jobs/apt_sync/probe.py::AptProbe.source_run::run_command": 1,
    "jobs/apt_sync/probe.py::AptProbe.target_run::run_command": 1,
    "jobs/apt_sync/probe.py::AptProbe.source_manual_names::run_command": 1,
    "jobs/apt_sync/probe.py::AptProbe.source_policy::run_command": 1,
    "jobs/apt_sync/probe.py::AptProbe.query_target_items::run_command": 1,
    "jobs/apt_sync/probe.py::AptProbe.collect_hold_sets::run_command": 2,
    "jobs/apt_sync/probe.py::AptProbe.capture_target_installed::run_command": 1,
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
    # (`sha256sum`, one batched read per scope), the target's $HOME, and capability probes.
    "jobs/flatpak_sync.py::FlatpakSyncJob.capture_source_items::run_command": 1,
    "jobs/flatpak_sync.py::FlatpakSyncJob.query_target_items::run_command": 1,
    "jobs/flatpak_sync.py::FlatpakSyncJob._capture_source_remotes::run_command": 2,
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
    # manual_installs_sync: unowned-file scan, apt queries, and the target's $HOME.
    "jobs/manual_installs_sync.py::ManualInstallsSyncJob._push_snippet_registry::run_command": 1,
    "jobs/manual_installs_sync.py::ManualInstallsSyncJob._scan_no_candidate_apt_packages::run_command": 1,
    "jobs/manual_installs_sync.py::ManualInstallsSyncJob._scan_unowned_installs::run_command": 2,
    "jobs/manual_installs_sync.py::ManualInstallsSyncJob._source_installed_names::run_command": 1,
    "jobs/manual_installs_sync.py::ManualInstallsSyncJob.validate::run_command": 2,
    # The decision file and snippet registry are read with `cat`; their writes are gated.
    "jobs/packages/state.py::DecisionFile.load::run_command": 1,
    "jobs/packages/state.py::SnippetRegistry.load::run_command": 1,
    "jobs/snap_sync.py::SnapSyncJob.capture_source_items::run_command": 1,
    "jobs/snap_sync.py::SnapSyncJob.query_target_items::run_command": 1,
    "jobs/snap_sync.py::SnapSyncJob.validate::run_command": 6,
    "jobs/vscode_state_sync.py::VscodeStateSyncJob.validate::run_command": 1,
    "jobs/vscode_state_sync.py::VscodeStateSyncJob.execute::run_command": 1,
    # `flock --nonblock <path> --command read` takes an advisory lock on a file already written above it;
    # the process holds it and changes no content.
    "lock.py::start_persistent_remote_lock::start_process": 1,
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

        This is the ratchet: a newly added write that forgets `mutates=` lands in a
        function whose expected count no longer matches, and fails here until it is either
        gated or justified.
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

        Kept separate from the ratchet above so the two failures read differently — that one
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
# requirements, and are listed only so this audit's ratchet stays closed over the whole
# package. A key here may never be under `_PACKAGE_SYNC_PATHS`, which is asserted.
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
        """J149 — the ratchet: a new write through a handle that is not the target's lands here.

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
