"""Generic folder sync job via rsync-over-SSH.

Syncs configured folders from source to target, running rsync as root on both
ends (local `sudo -E rsync` on source, `--rsync-path='sudo rsync'` on target)
to preserve cross-owner file metadata including POSIX ACLs and xattrs.

The key safety mechanism is the target-divergence guard in validate(): it uses
`btrfs subvolume find-new` to detect whether the target's synced subvolume was
independently modified since the last sync, aborting before the destructive
mirror+delete runs (D-06/D-07/D-08/D-18).
"""

from __future__ import annotations

import re
import shlex
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, ClassVar

from pcswitcher import config_sync, sync_history
from pcswitcher.jobs.base import SyncJob
from pcswitcher.models import Host, LogLevel, ProgressUpdate, ValidationError

# Matches rsync --info=progress2 output, e.g.:
#   "9.53G 21% 317.26MB/s 0:00:28 (xfr#83063, to-chk=443926/538653)"
# Group 1: size token (e.g. "9.53G") — used to compute bytes_transferred (WR-01).
# Group 2: percent complete.  Group 3: files transferred so far.  Group 4: total-to-check.
_PROGRESS2_RE = re.compile(r"(\d+[\d.]*[KMGT]?)\s+(\d+)%\s+\S+\s+\S+\s+\(xfr#(\d+),\s*to-chk=\d+/(\d+)\)")


class DivergenceStatus(Enum):
    """Result of a btrfs-based target-divergence check.

    CLEAN: no user changes found since the stored generation.
    DIVERGED: at least one user file changed.
    UNVERIFIABLE: the subvolume could not be resolved or find-new failed;
        the caller must fail closed when a stored baseline exists (CR-02).
    """

    CLEAN = "clean"
    DIVERGED = "diverged"
    UNVERIFIABLE = "unverifiable"


@dataclass
class FolderEntry:
    """A single folder to sync with its include/exclude configuration.

    Excludes map to rsync `--filter=- <pattern>` arguments in order (first-match-wins).
    """

    path: str
    enabled: bool = True
    excludes: list[str] = field(default_factory=list)

    def to_rsync_filter_args(self) -> list[str]:
        """Convert excludes to rsync --filter arguments.

        Returns a list of `--filter=- <pattern>` strings in config order.
        First-match-wins semantics are preserved because the order of the list
        is the order in which rsync evaluates the filter rules.
        """
        return [f"--filter=- {pattern}" for pattern in self.excludes]


class FolderSyncJob(SyncJob):
    """Generic folder sync via rsync-over-SSH.

    Syncs configured folders from source to target, running rsync as root on
    both ends to preserve cross-owner file metadata.  Enforces target-divergence
    check before mirror+delete to prevent data loss (D-06).

    Config shape (mirrors config-schema.yaml `folder_sync` section):
        folders:
          - path: /home
            enabled: true          # default
            excludes: [...]        # rsync filter patterns, optional
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
                        "excludes": {
                            "type": "array",
                            "items": {"type": "string"},
                            "default": [],
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

    @staticmethod
    def _parse_size_to_bytes(value: str) -> int:
        """Convert an rsync size token (e.g. '9.53G', '317K', '512') to an integer byte count.

        Multipliers: K=1024, M=1024**2, G=1024**3, T=1024**4.  A bare number (no suffix)
        is returned as-is.  Conversion is best-effort — rsync rounds progress figures, so
        the result is an approximation of the true cumulative byte count (WR-01).
        """
        multipliers: dict[str, int] = {"K": 1024, "M": 1024**2, "G": 1024**3, "T": 1024**4}
        if value and value[-1].upper() in multipliers:
            return int(float(value[:-1]) * multipliers[value[-1].upper()])
        return int(float(value))

    def _active_folders(self) -> list[FolderEntry]:
        """Return only enabled folder entries from config."""
        return [
            FolderEntry(
                path=f["path"],
                enabled=f.get("enabled", True),
                excludes=f.get("excludes", []),
            )
            for f in self.context.config["folders"]
            if f.get("enabled", True)
        ]

    async def validate(self) -> list[ValidationError]:
        """Validate system state before executing the folder sync.

        Checks (in order):
        1. `sudo rsync --version` succeeds on source and target.
        2. `acl` package is installed on source and target (required for -A flag;
           without it rsync silently drops ACLs — RESEARCH Pitfall 5).
        3. Each active folder path exists on the source.
        4. Target has not diverged since the last sync (D-06 divergence guard).

        Returns early after step 3 if any structural error was found, so the
        divergence guard never runs against a broken environment.
        """
        errors: list[ValidationError] = []

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

        # --- Step 3: active folder existence on source ---
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

        # Return early if structural errors found; the divergence guard requires a
        # working environment to produce meaningful results.
        if errors:
            return errors

        # --- Step 4: target-divergence guard (D-06/D-07/D-08/D-18) ---
        for folder in self._active_folders():
            error = await self._check_divergence(folder)
            if error is not None:
                errors.append(error)

        return errors

    def _build_rsync_cmd(self, folder: FolderEntry, dry_run: bool) -> str:
        """Build the rsync shell command for syncing a single folder.

        Produces a command that:
        - Runs as root on source via `sudo -E rsync` (preserves SSH_AUTH_SOCK and
          $HOME so root's rsync subprocess can reach the target's ~/.ssh/config —
          Pitfall 1 from RESEARCH.md).
        - Elevates to root on target via `--rsync-path='sudo rsync'` over the
          normal-user SSH connection (D-05); root SSH login stays disabled.
        - Uses the D-13 flag baseline: -aAXHS + --numeric-ids + --delete.
        - Adds --dry-run only when `dry_run` is True (D-12).
        - Never includes --delete-excluded (excluded files must survive on target
          — D-06) or --checksum (rsync's built-in verification is trusted — D-14).

        All config-derived values (folder path, exclude patterns, target hostname)
        are shlex.quote'd to prevent shell injection (T-05-01).
        """
        parts = [
            "sudo",
            "-E",
            "rsync",
            "-aAXHS",
            "--numeric-ids",
            "--delete",
            "--info=progress2",
            f"--out-format={shlex.quote('%i %n%L')}",
            "--partial",
            "--mkpath",
        ]

        if dry_run:
            parts.append("--dry-run")

        # SSH transport: no pseudo-tty (-T), quiet (-q) to suppress host banner.
        # sudo -E above preserves $HOME and SSH_AUTH_SOCK so the ssh subprocess
        # uses the invoking user's ~/.ssh/config and agent (Pitfall 1).
        parts.append(f"-e {shlex.quote('ssh -T -q')}")

        # Root on target via passwordless sudo scoped to /usr/bin/rsync (D-05).
        parts.append(f"--rsync-path={shlex.quote('sudo rsync')}")

        # Per-folder filter rules in config order (first-match-wins for rsync).
        # DO NOT add --delete-excluded — excluded machine-specific files (e.g.
        # .ssh/id_*, .config/tailscale) must survive on the target (D-06).
        parts.extend(f"--filter={shlex.quote(f'- {pattern}')}" for pattern in folder.excludes)

        # Source: trailing slash syncs contents, not the directory itself.
        src = shlex.quote(folder.path.rstrip("/") + "/")
        parts.append(src)

        # Destination: <target_hostname>:<path>/ — rsync opens its own SSH
        # connection using ~/.ssh/config (ADR-002); the colon is rsync's own
        # remote-host separator, not a shell metacharacter.
        dst_raw = f"{self.context.target_hostname}:{folder.path.rstrip('/') + '/'}"
        parts.append(shlex.quote(dst_raw))

        return " ".join(parts)

    async def _resolve_subvolume(self, path: str) -> tuple[str, str] | None:
        """Determine the btrfs subvolume mount and relative prefix for `path` on the target.

        Returns:
            (mount_point, relative_prefix) — e.g. ("/home", "") when /home is its own
            subvolume, or ("/", "root") when /root lives on the @ subvolume.
            Returns None if the btrfs subvolume cannot be determined (e.g. non-btrfs
            filesystem or unmounted path), which causes the divergence check to be
            skipped for this path (conservative fallback — RESEARCH Open Q3).
        """
        quoted = shlex.quote(path)
        result = await self.target.run_command(f"findmnt -no TARGET --target {quoted}", login_shell=False)
        if result.exit_code != 0 or not result.stdout.strip():
            return None

        mount = result.stdout.strip()
        # Compute the relative prefix of `path` within its btrfs subvolume mount.
        # e.g. path="/home", mount="/home" → prefix=""
        # e.g. path="/root", mount="/" → prefix="root"
        if path.rstrip("/") == mount.rstrip("/"):
            prefix = ""
        else:
            rel = path[len(mount.rstrip("/")) :]
            prefix = rel.lstrip("/")

        return mount, prefix

    async def _get_subvolume_generation(self, mount: str) -> int:
        """Query the current btrfs subvolume generation for `mount` on the target.

        Used by execute() (plan 05) to record the post-sync baseline via
        `sync_history.set_target_generation`.

        Args:
            mount: Absolute mount point of the btrfs subvolume, e.g. "/home".

        Returns:
            Generation integer parsed from `sudo btrfs subvolume show`.

        Raises:
            RuntimeError: If the generation line cannot be found in the output.
        """
        quoted = shlex.quote(mount)
        result = await self.target.run_command(f"sudo btrfs subvolume show {quoted}", login_shell=False)
        if result.exit_code != 0:
            raise RuntimeError(f"Cannot query btrfs generation for {mount!r}: {result.stderr.strip()}")
        for line in result.stdout.splitlines():
            if "Generation:" in line:
                return int(line.split()[-1])
        raise RuntimeError(f"Generation not found in `btrfs subvolume show {mount}` output: {result.stdout[:200]!r}")

    async def _target_diverged_since(self, folder: FolderEntry, stored_gen: int) -> DivergenceStatus:
        """Return the divergence status of the target's synced folder since `stored_gen`.

        Uses `btrfs subvolume find-new <mount> <stored_gen>` to list filesystem
        objects that changed after the stored generation.  The check is file-level:
        a changed file must lie under `prefix` (the folder's relative path within
        its subvolume mount).  Creating a read-only snapshot after the sync does
        not write files under the user data prefix, so it does not cause a false
        positive (D-07).

        For an empty prefix (subvolume root, the default `/home`/`/root` case),
        pc-switcher's own tool-state writes are excluded from the divergence scope:
        - `~/.local/share/pc-switcher/` (sync-history.json, lock) written by the
          post-sync baseline/role-record steps on the target.
        - `~/.config/pc-switcher/` (config.yaml) written by Phase-8 config sync
          before the job runs.
        These writes inevitably bump @home after the baseline is captured and are
        NOT user divergence (CR-01).  The filter is NOT applied for non-empty
        prefixes — a change under an arbitrary synced subfolder that happens to
        contain a `.local/share/pc-switcher/` subpath is real user data (Codex HIGH #2).

        Returns:
            CLEAN: no user changes since `stored_gen`.
            DIVERGED: at least one user file changed.
            UNVERIFIABLE: subvolume resolution or find-new command failed — the
                caller must fail closed when a stored baseline exists (CR-02).
        """
        resolved = await self._resolve_subvolume(folder.path)
        if resolved is None:
            self._log(
                Host.TARGET,
                LogLevel.WARNING,
                f"Divergence tracking unavailable for {folder.path!r}: "
                "could not resolve btrfs subvolume mount — treating as unverifiable (CR-02)",
            )
            return DivergenceStatus.UNVERIFIABLE

        mount, prefix = resolved
        quoted_mount = shlex.quote(mount)
        result = await self.target.run_command(
            f"sudo btrfs subvolume find-new {quoted_mount} {stored_gen}",
            login_shell=False,
        )
        if result.exit_code != 0:
            self._log(
                Host.TARGET,
                LogLevel.WARNING,
                f"find-new failed for {folder.path!r}: {result.stderr.strip()} — treating as unverifiable (CR-02)",
            )
            return DivergenceStatus.UNVERIFIABLE

        if prefix == "":
            # Empty prefix means this folder IS the subvolume root (default /home case).
            # pc-switcher's own post-sync writes land under @home and would bump the
            # generation — exclude them so they don't trigger false divergence (CR-01).
            # Strip the leading ~/ to get the path segment used in find-new output, then
            # add surrounding slashes so the match is a true path-component check.
            history_token = f"/{sync_history.HISTORY_DIR.lstrip('~/')}/"  # /.local/share/pc-switcher/
            config_token = f"/{config_sync.CONFIG_REMOTE_DIR.lstrip('~/')}/"  # /.config/pc-switcher/
            tool_state_tokens = (history_token, config_token)

            for line in result.stdout.splitlines():
                # The final summary line "transid marker was N" is not a file change.
                if line.startswith("transid marker"):
                    continue
                if not line.strip():
                    continue
                # Skip lines whose path lies under a pc-switcher-owned tool-state directory.
                # These are valid ONLY for the subvolume-root case — a user syncing a
                # non-empty subfolder that happens to contain such a path still gets
                # the change reported as real divergence (handled in the else branch).
                if any(token in line for token in tool_state_tokens):
                    continue
                return DivergenceStatus.DIVERGED
            return DivergenceStatus.CLEAN
        else:
            for line in result.stdout.splitlines():
                # The final summary line "transid marker was N" is not a file change.
                if line.startswith("transid marker"):
                    continue
                if not line.strip():
                    continue
                # Only include files whose path starts with the synced prefix.
                if f" {prefix}/" in line or line.endswith(f" {prefix}"):
                    return DivergenceStatus.DIVERGED
            return DivergenceStatus.CLEAN

    async def _check_divergence(self, folder: FolderEntry) -> ValidationError | None:
        """Run the divergence guard for a single folder.

        Returns a ValidationError when divergence blocks the sync, or None when
        the sync may proceed (first-sync, untouched target, or override active).

        Under dry_run or allow_divergence a detected divergence or unverifiable result
        is logged at WARNING and does NOT block (D-12, D-06 override).

        Fail-open vs fail-closed:
        - stored is None (never synced): fail-open — first sync proceeds (RESEARCH Open Q3).
        - stored is a real generation: run _target_diverged_since; CLEAN proceeds, DIVERGED
          and UNVERIFIABLE block (CR-02).
        - stored is UNKNOWN_GENERATION: short-circuit to UNVERIFIABLE without querying the
          target — the previous run could not establish a baseline; block to be safe (WR-02).
        """
        stored = sync_history.get_target_generation(self.context.target_hostname, folder.path)
        if stored is None:
            self._log(
                Host.TARGET,
                LogLevel.INFO,
                f"No divergence baseline for {folder.path!r} on "
                f"{self.context.target_hostname!r} — first sync, proceeding",
            )
            return None

        # UNKNOWN_GENERATION means the previous run transferred data but could not capture
        # the post-sync generation. Short-circuit to UNVERIFIABLE without querying the target
        # so the guard fails closed rather than silently skipping (CR-02 / WR-02 read path).
        if stored == sync_history.UNKNOWN_GENERATION:
            status = DivergenceStatus.UNVERIFIABLE
        else:
            status = await self._target_diverged_since(folder, stored)

        if status == DivergenceStatus.CLEAN:
            return None

        if status == DivergenceStatus.DIVERGED:
            msg = (
                f"Target divergence detected for {folder.path!r}: "
                f"{self.context.target_hostname!r} has been modified since the last sync. "
                "Re-run with --allow-divergence to proceed after manual review."
            )
        else:  # UNVERIFIABLE
            msg = (
                f"Target divergence state is unverifiable for {folder.path!r}: "
                f"the btrfs subvolume could not be queried on {self.context.target_hostname!r}. "
                "Re-run with --allow-divergence after manual review, or retry if the "
                "target was temporarily inaccessible."
            )

        if self.context.dry_run or self.context.allow_divergence:
            # Log at WARNING for the audit trail (T-04-02b) but do not block.
            self._log(Host.TARGET, LogLevel.WARNING, msg + " [proceeding]")
            return None

        return self._validation_error(Host.TARGET, msg)

    async def _stream_rsync(
        self,
        chunks: AsyncIterator[bytes],
        folder: FolderEntry,
    ) -> tuple[int, int, int]:
        """Stream rsync stdout, updating TUI progress and logging per-file events.

        Handles `--info=progress2` output (carriage-return-delimited) and
        `--out-format` per-file lines (newline-delimited) in the same byte
        stream (RESEARCH Pitfall 2 — do not use readline-based iteration here).

        Decoupled from the subprocess: consumes any `AsyncIterator[bytes]` so
        it can be unit-tested with a fake async generator.

        Args:
            chunks: Async byte-chunk source (e.g. `proc.read_stdout_chunks()`).
            folder: The folder being synced (used for log context).

        Returns:
            Tuple of (files_transferred, bytes_transferred, files_deleted).
            bytes_transferred is a best-effort count based on the last progress
            line (progress2 doesn't expose a precise cumulative byte count).
        """
        files_xfr = 0
        bytes_xfr = 0
        files_deleted = 0
        buf = b""

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
                    # Group numbering after adding the size capture group:
                    #   1=size  2=percent  3=files_xfr  4=total_check
                    # Last progress line wins — rsync emits these as running totals so the
                    # final line is the best approximation of the cumulative byte count.
                    bytes_xfr = self._parse_size_to_bytes(m.group(1))
                    pct = int(m.group(2))
                    files_xfr = int(m.group(3))
                    total_check = int(m.group(4))
                    self._report_progress(
                        ProgressUpdate(
                            percent=min(pct, 100),
                            current=files_xfr,
                            total=total_check,
                        )
                    )
                elif line[0] in (">", "<", "*", ".", "c", "h"):
                    # Per-file --out-format line (format: "%i %n%L") — log at FULL (D-16).
                    # Change-type characters (rsync %i first char):
                    #   > sent to remote   < received from remote   * special message
                    #   . attribute-only   c created (dir/symlink/device)   h hard link (IN-03)
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

    async def execute(self) -> None:
        """Sync each active folder via rsync-over-SSH.

        For each active folder (D-10):
        1. Builds the rsync command with the D-13 flag baseline, machine-specific
           filter rules (D-11), and the dry-run toggle (D-12).
        2. Spawns rsync as an async subprocess (ADR-005 — no blocking calls).
        3. Streams stdout through `_stream_rsync` for TUI progress (D-15) and
           per-file FULL logs (D-16).
        4. On non-zero exit, logs CRITICAL and raises RuntimeError (sync aborts).
        5. After ALL folders succeed, records the post-sync btrfs subvolume
           generation on the target as the next run's divergence baseline
           (D-06/D-08) — skipped in dry-run (D-12).
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

            # Re-check divergence immediately before the destructive transfer (WR-03 / T-09-01).
            # The Phase-4 validate() check runs earlier in the orchestrator pipeline; an arbitrary
            # user write arriving between Phase 4 and Phase 9 would otherwise escape the guard.
            # The target lock stops concurrent pc-switcher syncs but not unrelated user activity.
            # _check_divergence already encodes the dry_run/allow_divergence overrides (returns
            # None in those modes), so a non-None result here always means: block before --delete.
            # The empty-prefix tool-state filter (01-07) ensures a Phase-8 config.yaml write
            # (~/.config/pc-switcher/config.yaml) does NOT re-trigger divergence for /home.
            recheck_error = await self._check_divergence(folder)
            if recheck_error is not None:
                self._log(
                    Host.TARGET,
                    LogLevel.CRITICAL,
                    f"Pre-transfer divergence re-check blocked sync for {folder.path!r}: {recheck_error.message}",
                )
                raise RuntimeError(
                    f"Pre-transfer divergence re-check failed for {folder.path!r}: {recheck_error.message}"
                )

            cmd = self._build_rsync_cmd(folder, self.context.dry_run)

            # Spawn rsync; in dry_run mode the command already includes --dry-run
            # so rsync performs a real read-only preview without writing any files.
            proc = await self.source.start_process(cmd)

            # Stream progress and per-file output
            files_transferred, bytes_transferred, files_deleted = await self._stream_rsync(
                proc.read_stdout_chunks(), folder
            )

            # Obtain exit code and stderr after stdout is fully consumed
            result = await proc.wait_result()

            if result.exit_code != 0:
                self._log(
                    Host.SOURCE,
                    LogLevel.CRITICAL,
                    f"rsync failed for {folder.path!r}: {result.stderr.strip()}",
                    exit_code=result.exit_code,
                )
                raise RuntimeError(f"rsync failed for {folder.path!r} with exit code {result.exit_code}")

            # Per-folder summary (D-16)
            self._log(
                Host.SOURCE,
                LogLevel.INFO,
                f"{prefix}Completed sync of {folder.path!r}: "
                f"{files_transferred} files transferred, "
                f"{bytes_transferred} bytes, "
                f"{files_deleted} deletions",
            )

        # Record post-sync divergence baseline (D-06/D-08).
        # Skipped in dry-run to satisfy the tool-wide D-12 contract (no state writes).
        if not self.context.dry_run:
            for folder in folders:
                resolved = await self._resolve_subvolume(folder.path)
                if resolved is None:
                    self._log(
                        Host.TARGET,
                        LogLevel.WARNING,
                        f"Cannot record divergence baseline for {folder.path!r}: "
                        "could not resolve btrfs subvolume mount — "
                        "recording sentinel for fail-closed next run (WR-02)",
                    )
                    # Write the sentinel so the next run's guard fails closed rather than
                    # silently skipping (the successful rsync transfer must not be rolled back).
                    sync_history.set_target_generation(
                        self.context.target_hostname, folder.path, sync_history.UNKNOWN_GENERATION
                    )
                    continue
                mount, _ = resolved
                try:
                    gen = await self._get_subvolume_generation(mount)
                    sync_history.set_target_generation(self.context.target_hostname, folder.path, gen)
                except (RuntimeError, ValueError) as exc:
                    # _get_subvolume_generation raises RuntimeError on non-zero btrfs exit
                    # and ValueError if int(line.split()[-1]) fails on a malformed Generation
                    # line.  Neither should abort an otherwise-successful sync (WR-02).
                    self._log(
                        Host.TARGET,
                        LogLevel.WARNING,
                        f"Post-sync baseline capture failed for {folder.path!r}: {exc!s} — "
                        "recording sentinel so next run fails closed (WR-02)",
                    )
                    sync_history.set_target_generation(
                        self.context.target_hostname, folder.path, sync_history.UNKNOWN_GENERATION
                    )
