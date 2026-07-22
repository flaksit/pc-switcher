"""Generic folder sync job via rsync-over-SSH.

Syncs configured folders from source to target, running rsync as root on both
ends (local `sudo -E rsync` on source, `--rsync-path='sudo rsync'` on target)
to preserve cross-owner file metadata including POSIX ACLs and xattrs.

Safety relies on btrfs pre/post snapshots, the rsync --dry-run deletion log,
and the orchestrator topology check (ADR-015).  This module is a pure rsync
mirror: validate() checks system prerequisites only; execute() runs the
rsync transfer.
"""

from __future__ import annotations

import getpass
import os
import re
import shlex
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, ClassVar, override

from pcswitcher.disk import format_bytes
from pcswitcher.jobs.base import SyncJob
from pcswitcher.jobs.vscode_state_sync import vscode_state_exclude_paths
from pcswitcher.models import ConfigError, FirstSyncScope, Host, LogLevel, ProgressUpdate, ValidationError

# Matches rsync --info=progress2 output, e.g.:
#   "9.53G 21% 317.26MB/s 0:00:28 (xfr#83063, to-chk=443926/538653)"
#   "29,958,458  99%   27.90GB/s    0:00:00 (xfr#298201, to-chk=1200/300501)"
#   "20.000   0%    0,00kB/s    0:00:00 (xfr#1, ir-chk=1039/1101)"
# Group 1: size token (e.g. "9.53G" or "29,958,458") — used to compute
# bytes_transferred (WR-01).  rsync thousands-groups the plain byte counter with
# a locale-dependent separator (',' on C/en_US, '.' on nl_BE-style), so the size
# class must accept both: dropping ',' made re.search() stop at the first comma
# and capture only the last 1-3 digits (e.g. "29,958,458" -> "458"), skewing
# bytes_transferred by orders of magnitude while files-transferred (ungrouped)
# stayed correct.
# Group 2: rsync's own percentage — captured but NEVER shown.  It is bytes-sent over
# the total size of the whole tree, not of the files needing transfer, so an
# incremental sync reads 0% from first line to last (measured: 200 of 154,022 files
# re-sent, 14.4 MB against a 14.6 GB tree, 0% on all 206 lines).  The bar is driven by
# the checked-file counter instead, which spans 0-100% in every case.
# Group 3: files transferred so far.
# Group 4: chk prefix (`ir` or `to`).  Group 5: files still to check.  Group 6: total.
# The trailing counter is `ir-chk=` (incremental-recursion, file list still being
# built) or `to-chk=` (full list known).  Both must match: matching only to-chk left
# the bar hidden for most of a big first sync (#191), since no match means no
# `_report_progress` call and the Rich task is never lazily created.
#
# The prefix is captured because the two phases differ in signal quality: under ir-chk
# group 6 is the entry count discovered *so far*, not a total, so neither it nor any
# percentage derived from it may reach the bar.  `--no-inc-recursive` (see
# `_build_rsync_cmd`) means ir-chk should never appear at all; `_stream_rsync` keeps a
# degraded scanned-count path for it regardless.
_PROGRESS2_RE = re.compile(r"(\d+[\d,.]*[KMGT]?)\s+(\d+)%\s+\S+\s+\S+\s+\(xfr#(\d+),\s*(ir|to)-chk=(\d+)/(\d+)\)")

# TUI names for the two rsync passes a folder can run (see `execute`): the no-delete
# pass that ships per-directory filter files, and the deleting mirror.  Each pass gets
# its own progress bar, so both names stay on screen for the rest of the run.
PASS_PREP = "prep"
PASS_FULL = "full"

# pc-switcher's own runtime STATE lives under the invoking user's home and must
# stay machine-local: sync-history.json is the topology-safety state (ADR-015) that
# a --delete mirror would clobber mid-sync; the lock file and logs are per-machine.
# See ADR-017 (supersedes ADR-016).
#
# The install itself (uv tool venv + ~/.local/bin shim) is deliberately NOT
# excluded: it mirrors from source like any other uv tool, so the venv and the
# interpreter it references travel together and stay consistent. Excluding the venv
# while --delete-mirroring the uv interpreter tree it depends on deleted the in-use
# interpreter and broke pc-switcher on the target.
#
# Two GLOBAL-FIRST, non-overridable exclude groups exist, both owned elsewhere or here
# and both emitted before the user filter surfaces so no `+` rule can re-expose them:
#   1. this runtime-state relpath (below), home-relative and folder_sync's own concern;
#   2. the VS Code state DBs (VS Code and its forks), whose ABSOLUTE paths are owned and
#      computed by vscode_state_sync (which merges them selectively so machine-bound
#      secret:// rows are never clobbered, ADR-018). folder_sync knows nothing about VS Code
#      or home layout — it just translates each absolute path from vscode_state_exclude_paths()
#      into an rsync filter for the folder being synced (see _vscode_state_exclude_filters).
# Every OTHER exclusion is user-configurable.
_RUNTIME_EXCLUDE_RELPATHS: tuple[str, ...] = (
    ".local/share/pc-switcher",  # lock, sync-history.json, logs/
)


@dataclass
class FolderEntry:
    """A single folder to sync, with its optional central rsync filter file.

    `filter_file` is a per-folder central rsync merge filter file (native
    rsync `+`/`-` filter syntax, first-match-wins); unset/empty means no
    central merge rule for that folder (runtime excludes and the tree-wide
    `dir-merge /.pcswitcher-filter` still apply).
    """

    path: str
    enabled: bool = True
    filter_file: str | None = None

    def expanded_filter_file(self) -> str | None:
        """Return the ~- and env-var-expanded absolute filter_file path, or None if unset.

        Expansion uses the invoking user's environment because the source is
        always the local machine and rsync reads the merge file locally there
        (the merge/dir-merge rules only take effect on the source side of the
        transfer; see `_build_rsync_cmd`).
        """
        if not self.filter_file:
            return None
        # Path.expanduser() has no env-var equivalent, so expandvars runs first (os.path,
        # no pathlib alternative exists), then Path.expanduser() resolves the leading ~.
        return str(Path(os.path.expandvars(self.filter_file)).expanduser())


class FolderSyncJob(SyncJob):
    """Generic folder sync via rsync-over-SSH.

    Syncs configured folders from source to target, running rsync as root on
    both ends to preserve cross-owner file metadata.

    Config shape (mirrors config-schema.yaml `folder_sync` section):
        folders:
          - path: /home
            enabled: true          # default
            filter_file: ~/...     # central rsync merge filter file, optional
    """

    name: ClassVar[str] = "folder_sync"

    CONFIG_SCHEMA: ClassVar[dict[str, Any]] = {
        "type": "object",
        "properties": {
            "folders": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "path": {"type": "string"},
                        "enabled": {"type": "boolean", "default": True},
                        "filter_file": {
                            "type": "string",
                            "description": (
                                "Path to a central rsync merge filter file for this folder "
                                "(~ and env vars expanded); unset means no central filter rule"
                            ),
                        },
                    },
                    "required": ["path"],
                    "additionalProperties": False,
                },
                "minItems": 1,
            }
        },
        "required": ["folders"],
        "additionalProperties": False,
    }

    @classmethod
    @override
    def validate_config(cls, config: dict[str, Any]) -> list[ConfigError]:
        """Validate config schema, then that every folder path is absolute.

        `path` is handed to rsync verbatim — no `~`/env expansion, unlike
        `filter_file` — so a relative path silently resolves against the working
        directory of each side and mirrors the wrong tree.
        """
        errors = super().validate_config(config)
        if errors:
            return errors  # Don't continue if schema is invalid

        for index, folder in enumerate(config["folders"]):
            if not PurePosixPath(folder["path"]).is_absolute():
                errors.append(
                    ConfigError(
                        job=cls.name,
                        path=f"folders.{index}.path",
                        message=f"Folder path must be absolute: {folder['path']!r}",
                    )
                )

        return errors

    @staticmethod
    def _parse_size_to_bytes(value: str) -> int:
        """Convert an rsync progress figure (e.g. '9.53G', '317K', '80,153,795') to bytes.

        Two shapes occur in --info=progress2 output:
        - the plain byte counter, which rsync thousands-groups with a
          locale-dependent separator (',' on C/en_US, '.' on nl_BE-style),
          e.g. '80,153,795';
        - a human-readable figure with a K/M/G/T suffix (only when rsync runs
          with -h), e.g. '9.53G', where the '.' is a decimal point.

        The parser strips both grouping separators so any locale's counter parses:
        the byte count is best-effort progress metadata (WR-01), not sync-critical.

        Multipliers: K=1024, M=1024**2, G=1024**3, T=1024**4.
        """
        multipliers: dict[str, int] = {"K": 1024, "M": 1024**2, "G": 1024**3, "T": 1024**4}
        if value and value[-1].upper() in multipliers:
            # Suffixed figure carries a single decimal point; strip any comma grouping.
            return int(float(value[:-1].replace(",", "")) * multipliers[value[-1].upper()])
        # Plain integer counter: every '.'/',' is a thousands separator.
        return int(value.replace(".", "").replace(",", "") or "0")

    def _active_folders(self) -> list[FolderEntry]:
        """Return only enabled folder entries from config."""
        return [
            FolderEntry(
                path=f["path"],
                enabled=f.get("enabled", True),
                filter_file=f.get("filter_file"),
            )
            for f in self.context.config["folders"]
            if f.get("enabled", True)
        ]

    @classmethod
    @override
    def describe_first_sync_scope(cls, config: dict[str, Any]) -> FirstSyncScope | None:
        """Describe the enabled folder paths this job would overwrite on a first sync.

        Reproduces the same enabled-folder filter as `_active_folders`, but operates
        on a raw config dict (this is a classmethod, called before job instances
        exist) rather than `self.context.config`: an entry is in scope when it is a
        dict, its `enabled` (default True) is truthy, and its `path` is a str.
        """
        folders = config.get("folders", [])
        paths = [
            f["path"]
            for f in folders
            if isinstance(f, dict) and f.get("enabled", True) and isinstance(f.get("path"), str)
        ]
        if not paths:
            return None
        return FirstSyncScope(
            job_name=cls.name,
            scope_items=paths,
            mechanism="rsync --delete",
        )

    async def validate(self) -> list[ValidationError]:
        """Validate system state before executing the folder sync.

        Checks (in order):
        1. `sudo rsync --version` succeeds on source and target.
        2. `acl` package is installed on source and target (required for -A flag;
           without it rsync silently drops ACLs — RESEARCH Pitfall 5).
        3. Each active folder path exists on the source; if a folder configures a
           `filter_file`, its (expanded) path must also exist on the source, else
           a configured-but-absent filter file could silently degrade to a full
           `--delete` mirror with no filter rules applied.
        4. Source and target share the same CPU architecture (`uname -m`): the
           pc-switcher install mirrors as arch-specific binaries (ADR-017), so a
           heterogeneous fleet is unsupported.

        Returns the list of validation errors (empty on success).
        """
        errors: list[ValidationError] = []

        # --- Step 0: source/target CPU architecture match ---
        # The pc-switcher install (uv tool venv + interpreter) mirrors to the target
        # as arch-specific binaries (ADR-017). A heterogeneous fleet would ship a
        # wrong-arch interpreter that dies at exec time with a cryptic
        # "cannot execute: required file not found"; refuse it up front. Fail open if
        # either `uname -m` cannot be read (never determined arch → nothing to compare).
        src_arch = await self.source.run_command("uname -m")
        tgt_arch = await self.target.run_command("uname -m", login_shell=False)
        if src_arch.success and tgt_arch.success and src_arch.stdout.strip() != tgt_arch.stdout.strip():
            errors.append(
                self._validation_error(
                    Host.TARGET,
                    f"source/target CPU architecture mismatch "
                    f"({src_arch.stdout.strip()} vs {tgt_arch.stdout.strip()}): pc-switcher mirrors "
                    f"arch-specific binaries and does not support a heterogeneous fleet",
                )
            )

        # --- Step 1: sudo rsync availability ---
        result = await self.source.run_command("sudo rsync --version")
        if result.exit_code != 0:
            errors.append(
                self._validation_error(
                    Host.SOURCE,
                    "sudo rsync is not available on source (required for metadata-preserving transfer)",
                )
            )

        result = await self.target.run_command("sudo rsync --version", login_shell=False)
        if result.exit_code != 0:
            errors.append(
                self._validation_error(
                    Host.TARGET,
                    "sudo rsync is not available on target (required for metadata-preserving transfer)",
                )
            )

        # --- Step 2: acl package on both ends ---
        result = await self.source.run_command("dpkg -l acl | grep -q '^ii'")
        if result.exit_code != 0:
            errors.append(
                self._validation_error(
                    Host.SOURCE,
                    "acl package is not installed on source "
                    "(required for -A flag; without it rsync silently drops ACLs)",
                )
            )

        result = await self.target.run_command("dpkg -l acl | grep -q '^ii'", login_shell=False)
        if result.exit_code != 0:
            errors.append(
                self._validation_error(
                    Host.TARGET,
                    "acl package is not installed on target "
                    "(required for -A flag; without it rsync silently drops ACLs)",
                )
            )

        # --- Step 3: active folder existence on source, and filter_file existence ---
        for folder in self._active_folders():
            quoted = shlex.quote(folder.path)
            result = await self.source.run_command(f"test -d {quoted}")
            if result.exit_code != 0:
                errors.append(
                    self._validation_error(
                        Host.SOURCE,
                        f"folder {folder.path!r} does not exist on source",
                    )
                )

            expanded_filter_file = folder.expanded_filter_file()
            if expanded_filter_file is not None:
                # Check the EXPANDED path so no literal `~` reaches the shell (mirrors
                # the folder existence check above for consistency and correct host).
                quoted_filter = shlex.quote(expanded_filter_file)
                result = await self.source.run_command(f"test -f {quoted_filter}")
                if result.exit_code != 0:
                    errors.append(
                        self._validation_error(
                            Host.SOURCE,
                            f"filter_file {folder.filter_file!r} for folder {folder.path!r} does not exist on source",
                        )
                    )

        return errors

    @staticmethod
    def _runtime_exclude_filters(folder_path: str) -> list[str]:
        """rsync `--filter` args protecting pc-switcher's own runtime state.

        The runtime files (`_RUNTIME_EXCLUDE_RELPATHS`) live under the invoking user's
        home directory. When `folder_path` contains that home (e.g. syncing `/home`), each
        protected path is emitted as a root-anchored rsync pattern relative to the transfer
        root, so it matches exactly one location and can never be re-exposed by a user
        include rule. When home is outside `folder_path` (e.g. `/root` synced by a normal
        user) there is nothing to protect and the list is empty.

        The editor state DBs are excluded separately (`_vscode_state_exclude_filters`);
        they are owned by `vscode_state_sync`, not folder_sync.
        """
        try:
            rel = Path.home().relative_to(folder_path.rstrip("/") or "/")
        except ValueError:
            return []
        prefix = "/" if str(rel) == "." else f"/{rel}/"
        return [f"--filter={shlex.quote(f'- {prefix}{sub}')}" for sub in _RUNTIME_EXCLUDE_RELPATHS]

    @staticmethod
    def _vscode_state_exclude_filters(folder_path: str) -> list[str]:
        """rsync `--filter` args excluding the VS Code state DBs that fall under `folder_path`.

        The absolute paths come from `vscode_state_exclude_paths()` — `vscode_state_sync`
        owns which DBs to exclude (the invoking user's, resolved at call time); folder_sync
        only translates each absolute path into a root-anchored, first-match exclude
        relative to the transfer root. A path outside `folder_path` (e.g. the invoking
        user's `/home/<user>/…` DB when syncing `/root`) is skipped. Emitted GLOBAL-FIRST,
        before the user filter surfaces, so no `+` rule can re-expose the DBs. Scope is the
        invoking user only (ADR-018); folder_sync holds no VS Code/home knowledge itself.
        """
        root = Path(folder_path.rstrip("/") or "/")
        filters: list[str] = []
        for abs_path in vscode_state_exclude_paths():
            try:
                rel = abs_path.relative_to(root)
            except ValueError:
                continue  # DB not under this folder — nothing to exclude here.
            filters.append(f"--filter={shlex.quote(f'- /{rel}')}")
        return filters

    def _transport_args(self) -> list[str]:
        """Build the rsync `-e <ssh>` transport and `--rsync-path='sudo rsync'` args.

        Shared by the main mirror (`_build_rsync_cmd`) and the filter pre-seed pass
        (`_build_preseed_cmd`) so both use identical SSH credentials and target-side
        sudo elevation.

        SSH transport note: `sudo -E rsync` runs rsync as root, and the ssh it spawns
        also runs as root.  OpenSSH resolves `~/.ssh` from the running uid's passwd
        entry (root → /root/.ssh) regardless of the $HOME variable.  Passing explicit
        credentials (-l, -i, -o UserKnownHostsFile=, -F) with the invoking user's paths
        is the correct fix; relying on $HOME does NOT work (empirically verified on
        target VMs: ssh fails with "Host key verification failed" / rsync exit 255
        without explicit credentials).
        """
        ssh_dir = Path.home() / ".ssh"
        target_user = self.context.target_username or getpass.getuser()

        ssh_tokens: list[str] = ["ssh", "-T", "-q", "-l", target_user]

        # -F before host-name flags so it takes effect for all connection params.
        config_file = ssh_dir / "config"
        if config_file.exists():
            ssh_tokens += ["-F", str(config_file)]

        known_hosts_file = ssh_dir / "known_hosts"
        if known_hosts_file.exists():
            ssh_tokens += ["-o", f"UserKnownHostsFile={known_hosts_file}"]

        # Offer all default key types that exist; ssh tries each in order.
        # SSH_AUTH_SOCK (preserved by sudo -E) is also available when an agent
        # is running, but explicit -i keys cover the no-agent case.
        for key_name in ("id_ed25519", "id_ecdsa", "id_rsa"):
            key_file = ssh_dir / key_name
            if key_file.exists():
                ssh_tokens += ["-i", str(key_file)]

        # shlex.join quotes individual tokens (handles spaces in paths).
        # shlex.quote wraps the assembled command for the outer -e argument.
        ssh_cmd = shlex.join(ssh_tokens)
        # Root on target via passwordless sudo scoped to /usr/bin/rsync (D-05).
        return [f"-e {shlex.quote(ssh_cmd)}", f"--rsync-path={shlex.quote('sudo rsync')}"]

    async def _needs_seeding_pass(self, folder: FolderEntry) -> bool:
        """Whether a no-delete seeding pass must run before the deleting mirror.

        A dir-merge rule is read from the *receiver* during the delete scan, so the
        deleting mirror protects the target correctly only when every source
        `.pcswitcher-filter` is already present and byte-identical on the target.  In
        the steady state that already holds — filter files sync like any other file —
        so a single pass is safe and the whole tree is walked only once.  A seeding
        pass is needed only when a per-directory filter was added or changed on the
        source (or on a first sync): then the source's filter files are not yet
        reflected on the target and the mirror must align them first.

        Detects this by hashing just the (few, small) `.pcswitcher-filter` files on
        each side and comparing — far cheaper than the second full rsync pass it
        avoids.  Short-circuits when the source has none (the common case — the
        default config uses only the central `filter_file`), skipping the target
        round-trip.  Runs as root, like the mirror, so filter files under directories
        the invoking user cannot read are still seen and hashed.  Target-only extra
        filter files do not force a seeding pass (they only ever add protection).
        """
        path = shlex.quote(folder.path.rstrip("/") or "/")
        manifest_cmd = f"sudo find {path} -name .pcswitcher-filter -exec sha256sum {{}} +"
        src = await self.source.run_command(manifest_cmd)
        src_manifest = set(src.stdout.splitlines())
        if not src_manifest:
            return False  # no per-directory filters on the source -> nothing to seed
        tgt = await self.target.run_command(manifest_cmd, login_shell=False)
        tgt_manifest = set(tgt.stdout.splitlines())
        # Seed unless every source filter file is present and identical on the target.
        return not src_manifest.issubset(tgt_manifest)

    def _build_rsync_cmd(self, folder: FolderEntry, dry_run: bool, delete: bool = True) -> str:
        """Build the rsync shell command for syncing a single folder.

        Produces a command that:
        - Runs as root on source via `sudo -E rsync` (preserves SSH_AUTH_SOCK
          so the ssh agent socket remains accessible).
        - Elevates to root on target via `--rsync-path='sudo rsync'` over the
          normal-user SSH connection (D-05); root SSH login stays disabled.
        - Uses the D-13 flag baseline: -aAXHS + --numeric-ids + --delete.
        - Adds --dry-run only when `dry_run` is True (D-12).
        - Omits --delete when `delete` is False: `execute` runs a no-delete seeding
          pass first (when the tree has per-directory filters) so every source
          `.pcswitcher-filter` reaches the target before the deleting mirror — a
          dir-merge rule only protects the target once the filter file is on the
          receiver, else --delete would remove (not protect) the files it names.
        - Never includes --delete-excluded (excluded files must survive on target
          — D-06) or --checksum (rsync's built-in verification is trusted — D-14).

        All config-derived values (folder path, exclude patterns, target hostname)
        are shlex.quote'd to prevent shell injection (T-05-01).

        The SSH transport and target-side sudo elevation are built by
        `_transport_args`.  The filter chain (runtime excludes → central merge →
        dir-merge) is identical with and without --delete, so both passes respect the
        central `filter_file` and per-directory `.pcswitcher-filter` files exactly.
        """
        parts = [
            "sudo",
            "-E",
            "rsync",
            "-aAXHS",
            "--numeric-ids",
            # Build the whole file list before transferring anything, so
            # --info=progress2 reports against the real total from its first line.
            # Under rsync's default incremental recursion the walk and the transfer
            # interleave: the percentage is bytes-done over bytes-known-*so far* and
            # walks backwards as the walk discovers more tree, and `to-chk` only
            # appears for the handful of files still queued when the walk ends
            # (measured on a 14.6 GB / 154k-file tree: 138,535 ir-chk lines carrying
            # 99.9% of the bytes, then 1,159 to-chk lines in the final second).
            # Cost is bounded by the walk: measured on a 1.59M-entry /home, +0.5s wall
            # and +80 MB RSS, with no progress output at all until the walk finishes.
            "--no-inc-recursive",
            "--info=progress2",
            f"--out-format={shlex.quote('%i %n%L')}",
            "--partial",
            "--mkpath",
        ]

        if delete:
            parts.append("--delete")

        if dry_run:
            parts.append("--dry-run")

        # SSH transport + target-side sudo elevation (shared with the pre-seed pass).
        parts.extend(self._transport_args())

        # GLOBAL-FIRST filter precedence: the un-overridable excludes come first —
        # pc-switcher's runtime state (ADR-017) followed by the VS Code state DBs
        # (ADR-018); the folder's central `merge` filter (when configured) comes
        # next so its rules win over any per-directory file under first-match-wins;
        # the tree-wide `dir-merge /.pcswitcher-filter` is always last and gives
        # users a per-directory (gitignore-like) authoring surface. DO NOT add
        # --delete-excluded — excluded files (e.g. .ssh/id_*, .config/tailscale)
        # must survive on the target (D-06).
        parts.extend(self._runtime_exclude_filters(folder.path))
        parts.extend(self._vscode_state_exclude_filters(folder.path))

        expanded_filter_file = folder.expanded_filter_file()
        if expanded_filter_file is not None:
            parts.append(f"--filter={shlex.quote(f'merge {expanded_filter_file}')}")
        parts.append(f"--filter={shlex.quote('dir-merge /.pcswitcher-filter')}")

        # Source: trailing slash syncs contents, not the directory itself.
        src = shlex.quote(folder.path.rstrip("/") + "/")
        parts.append(src)

        # Destination: <target_hostname>:<path>/ — rsync opens its own SSH
        # connection using ~/.ssh/config (ADR-002); the colon is rsync's own
        # remote-host separator, not a shell metacharacter.
        dst_raw = f"{self.context.target_hostname}:{folder.path.rstrip('/') + '/'}"
        parts.append(shlex.quote(dst_raw))

        return " ".join(parts)

    async def _stream_rsync(
        self,
        chunks: AsyncIterator[bytes],
        folder: FolderEntry,
        label: str,
    ) -> tuple[int, int, int]:
        """Stream rsync stdout, updating TUI progress and logging per-file events.

        Handles `--info=progress2` output (carriage-return-delimited) and
        `--out-format` per-file lines (newline-delimited) in the same byte
        stream (RESEARCH Pitfall 2 — do not use readline-based iteration here).

        Decoupled from the subprocess: consumes any `AsyncIterator[bytes]` so
        it can be unit-tested with a fake async generator.

        Progress is reported per pass, not per job: the bar restarts at 0% for every
        folder and every pass, identified by `label`.  rsync is silent while it builds
        the file list (`--no-inc-recursive`, see `_build_rsync_cmd`), so the pass opens
        with a heartbeat and the bar only becomes determinate once transfer starts.

        Args:
            chunks: Async byte-chunk source (e.g. `proc.read_stdout_chunks()`).
            folder: The folder being synced (used for log context).
            label: Pass name shown next to the folder in the TUI (`prep` / `full`).

        Returns:
            Tuple of (files_transferred, bytes_transferred, files_deleted).
            bytes_transferred is a best-effort count based on the last progress
            line (progress2 doesn't expose a precise cumulative byte count).
        """
        files_xfr = 0
        bytes_xfr = 0
        files_deleted = 0
        files_scanned = 0
        buf = b""

        where = f"{folder.path} ({label})"
        self._report_progress(ProgressUpdate(heartbeat=True, item=f"{where} — building file list", track=where))

        async for chunk in chunks:
            buf += chunk
            # Split on both \r and \n to handle progress2 (uses \r) and
            # per-file --out-format lines (use \n) in the same stream.
            parts = re.split(rb"[\r\n]", buf)
            buf = parts[-1]  # Keep incomplete trailing fragment for next chunk.
            for fragment in parts[:-1]:
                line = fragment.decode(errors="replace").strip()
                if not line:
                    continue
                m = _PROGRESS2_RE.search(line)
                if m:
                    # Progress2 line: update TUI (D-15) and capture transferred bytes (WR-01).
                    # Groups: 1=size 2=percent (unused, see below) 3=files_xfr
                    #         4=chk prefix 5=to-check 6=total
                    # Last progress line wins — rsync emits these as running totals so the
                    # final line is the best approximation of the cumulative byte count.
                    bytes_xfr = self._parse_size_to_bytes(m.group(1))
                    files_xfr = int(m.group(3))
                    scanning = m.group(4) == "ir"
                    remaining = int(m.group(5))
                    listed = int(m.group(6))
                    if scanning:
                        # Fallback path only: --no-inc-recursive means rsync should never
                        # emit ir-chk.  If it does anyway, `listed` is the entry count
                        # discovered *so far* rather than a total, so neither it nor the
                        # percentage may be shown — report the scanned count alone and let
                        # the TUI render an indeterminate bar.  The count is clamped
                        # monotonic because `listed` grows under the walk.
                        files_scanned = max(files_scanned, listed - remaining)
                        self._report_progress(
                            ProgressUpdate(
                                current=files_scanned,
                                item=f"{where} — scanning {files_scanned:,} files ({format_bytes(bytes_xfr)})",
                                track=where,
                            )
                        )
                    else:
                        # Full file list known, so `listed` is the real total and the
                        # checked-file count drives the bar.  rsync's own percentage
                        # (group 2) is deliberately ignored: it is bytes-sent over the
                        # total size of the whole tree, so an incremental sync — the
                        # common case — reads 0% from start to finish (measured: 200 of
                        # 154,022 files re-sent, 14.4 MB of 14.6 GB, 0% on every line).
                        # Counting files instead spans 0-100% in both cases; the cost is
                        # that one huge file advances the bar as little as one tiny one,
                        # so the byte figure stays in the label.
                        checked = listed - remaining
                        self._report_progress(
                            ProgressUpdate(
                                percent=100 if listed == 0 else min(100, checked * 100 // listed),
                                item=f"{where} — {checked:,}/{listed:,} files, {format_bytes(bytes_xfr)}",
                                track=where,
                            )
                        )
                elif line[0] in (">", "<", "*", ".", "c", "h"):
                    # Per-file --out-format line (format: "%i %n%L") — log at FULL (D-16).
                    # Change-type characters (rsync %i first char):
                    #   > sent to remote   < received from remote   * special message
                    #   . attribute-only   c created (dir/symlink/device)   h hard link (IN-03)
                    # Full itemize-code reference: docs/reading-sync-logs.md
                    self._log(Host.SOURCE, LogLevel.FULL, f"{folder.path}: {line}")
                    if line.startswith("*deleting"):
                        files_deleted += 1

        # Flush any remaining fragment that didn't end with \r or \n.
        if buf:
            line = buf.decode(errors="replace").strip()
            if line and line[0] in (">", "<", "*", ".", "c", "h"):
                self._log(Host.SOURCE, LogLevel.FULL, f"{folder.path}: {line}")
                if line.startswith("*deleting"):
                    files_deleted += 1

        return files_xfr, bytes_xfr, files_deleted

    async def _run_rsync_pass(self, cmd: str, folder: FolderEntry, label: str) -> tuple[int, int, int]:
        """Run one rsync pass: spawn, stream progress/logs, and raise on non-zero exit.

        Spawns rsync as an async subprocess (ADR-005 — no blocking calls), streams
        stdout through `_stream_rsync` for TUI progress (D-15) and per-file FULL logs
        (D-16), then checks the exit code.  Returns the pass's
        (files_transferred, bytes_transferred, files_deleted).  Shared by the no-delete
        seeding pass and the deleting mirror in `execute`, which name themselves via
        `label` (`prep` / `full`).
        """
        proc = await self.source.start_process(cmd)
        files_transferred, bytes_transferred, files_deleted = await self._stream_rsync(
            proc.read_stdout_chunks(), folder, label
        )
        result = await proc.wait_result()
        if result.exit_code != 0:
            self._log(
                Host.SOURCE,
                LogLevel.CRITICAL,
                f"rsync failed for {folder.path!r}: {result.stderr.strip()}",
                exit_code=result.exit_code,
            )
            raise RuntimeError(f"rsync failed for {folder.path!r} with exit code {result.exit_code}")
        # A pass with nothing to transfer (steady state, or any dry-run) races through
        # its progress output, so close the bar explicitly rather than leaving it wherever
        # the last line landed — or pulsing, if no line ever arrived.
        where = f"{folder.path} ({label})"
        self._report_progress(ProgressUpdate(percent=100, item=where, track=where))
        return files_transferred, bytes_transferred, files_deleted

    async def execute(self) -> None:
        """Sync each active folder via rsync-over-SSH.

        For each active folder (D-10):
        1. When this is a real sync and the source's per-directory `.pcswitcher-filter`
           files are not already reflected on the target (`_needs_seeding_pass`), runs
           a seeding pass first — the same mirror WITHOUT `--delete` — so every source
           `.pcswitcher-filter` reaches the target before the deleting mirror.  A
           dir-merge rule only protects the target once the filter file is on the
           receiver, so without this the deleting mirror would remove (not protect)
           the files a per-dir rule names.  Both passes apply the identical filter
           chain, so the central `filter_file` and per-directory rules are respected
           exactly.  Skipped in dry-run (must not write to the target, D-12), which
           makes the dry-run deletion preview pessimistic for per-dir-protected paths
           (the safe direction), and skipped when the per-directory filters already
           match on both ends (the steady state — no seeding is needed).
        2. Runs the deleting mirror (D-13 baseline, D-11 filters, D-12 dry-run toggle).
        3. On non-zero exit from either pass, logs CRITICAL and raises RuntimeError.
        """
        folders = self._active_folders()

        for folder in folders:
            prefix = "[dry-run] " if self.context.dry_run else ""
            self._log(
                Host.SOURCE,
                LogLevel.INFO,
                f"{prefix}Syncing {folder.path!r} → {self.context.target_hostname!r}",
                session_id=self.context.session_id,
            )

            seed_files = 0
            seed_bytes = 0
            if not self.context.dry_run and await self._needs_seeding_pass(folder):
                self._log(
                    Host.SOURCE,
                    LogLevel.FULL,
                    f"Seeding per-directory filter files for {folder.path!r} (no-delete pass)",
                )
                seed_files, seed_bytes, _ = await self._run_rsync_pass(
                    self._build_rsync_cmd(folder, dry_run=False, delete=False), folder, PASS_PREP
                )

            # Deleting mirror (or, in dry-run, the read-only preview).
            mirror_files, mirror_bytes, files_deleted = await self._run_rsync_pass(
                self._build_rsync_cmd(folder, self.context.dry_run, delete=True), folder, PASS_FULL
            )

            # On a seeding run the bulk transfer happened in the first pass and the
            # mirror transfers ~nothing; sum so the summary reflects the real work.
            files_transferred = seed_files + mirror_files
            bytes_transferred = seed_bytes + mirror_bytes

            # Per-folder summary (D-16). Human-readable size at INFO; exact byte
            # count kept at DEBUG for precise diagnostics (#189).
            self._log(
                Host.SOURCE,
                LogLevel.INFO,
                f"{prefix}Completed sync of {folder.path!r}: "
                f"{files_transferred} files transferred, "
                f"{format_bytes(bytes_transferred)}, "
                f"{files_deleted} deletions",
            )
            self._log(
                Host.SOURCE,
                LogLevel.DEBUG,
                f"{prefix}Transferred {bytes_transferred} bytes for {folder.path!r}",
            )

        # No job-level completion update: every pass already closes its own bar at 100%
        # (`_run_rsync_pass`), and an unlabelled one here would only strip the folder
        # and pass name off the finished bar.
