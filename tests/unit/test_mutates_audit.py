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
    "jobs/apt_sync.py::simulate_apt_transaction::run_command": 1,
    "jobs/apt_sync.py::compare_deb_versions::run_command": 2,
    "jobs/apt_sync.py::AptSyncJob.capture_source_items::run_command": 1,
    "jobs/apt_sync.py::AptSyncJob._source_policy::run_command": 1,
    "jobs/apt_sync.py::AptSyncJob.query_target_items::run_command": 1,
    "jobs/apt_sync.py::AptSyncJob.query_target_items.run::run_command": 1,
    "jobs/apt_sync.py::AptSyncJob.collect_hold_sets::run_command": 2,
    "jobs/apt_sync.py::AptSyncJob.collect_target_policy::run_command": 1,
    # The post-`apt-get update` origin verification (ADR-021 D-35): one batched
    # `apt-cache policy` re-read of the converged target, which refuses installs but
    # changes nothing itself.
    "jobs/apt_sync.py::AptSyncJob._verify_approved_origins::run_command": 1,
    "jobs/apt_sync.py::AptSyncJob._plan_repo_diffs.source_run::run_command": 1,
    "jobs/apt_sync.py::AptSyncJob._plan_repo_diffs.target_run::run_command": 1,
    # The key-directory digests and the two source-file reference scans, captured ahead of
    # the package diff because the origin classification consumes them (ADR-021 D-34).
    "jobs/apt_sync.py::AptSyncJob._capture_origin_state.source_run::run_command": 1,
    "jobs/apt_sync.py::AptSyncJob._capture_origin_state.target_run::run_command": 1,
    # The post-write re-scan of the target's source files that keyring collection counts
    # references against — a `find ... -exec awk` read, no different from the plan-time one.
    "jobs/apt_sync.py::AptSyncJob._remove_unused_keyrings.target_run::run_command": 1,
    "jobs/apt_sync.py::AptSyncJob._capture_target_manual_set::run_command": 1,
    "jobs/apt_sync.py::AptSyncJob._backup_destination::run_command": 1,
    "jobs/apt_sync.py::AptSyncJob._target_home_dir::run_command": 1,
    "jobs/apt_sync.py::AptSyncJob.validate::run_command": 5,
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
    # Remote derivation's two source-side inputs: every installed ref including runtimes,
    # and the runtime each app is built against (one local `flatpak info` per app).
    "jobs/flatpak_sync.py::FlatpakSyncJob._capture_source_ref_origins::run_command": 1,
    "jobs/flatpak_sync.py::FlatpakSyncJob._capture_source_runtimes::run_command": 1,
    # The post-install origin read-back: the same `flatpak list` the capture uses, re-run on
    # the target so a ref's real provenance is checked rather than inferred (ADR-021 D-35).
    "jobs/flatpak_sync.py::FlatpakSyncJob._installed_origin_refusal::run_command": 1,
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
    "jobs/manual_installs_sync.py::ManualInstallsSyncJob.capture_source_items::run_command": 1,
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
        """Guard against the audit silently matching nothing (moved package, renamed
        methods): a rubber-stamp audit is worse than none."""
        ungated = _collect_ungated()
        assert len(ungated) > 40, f"only {len(ungated)} ungated call sites found — the AST walk is not finding them"

    def test_no_ungated_call_site_is_unaccounted_for(self) -> None:
        """Every executor call without `mutates=` is listed above as a read or a known gap.

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
        """The requirement: a write either carries `mutates=` or has an issue saying why not.

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
