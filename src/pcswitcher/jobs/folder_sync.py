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
import re
import shlex
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, ClassVar, override

from pcswitcher.jobs.base import SyncJob
from pcswitcher.models import FirstSyncScope, Host, LogLevel, ProgressUpdate, ValidationError

# Matches rsync --info=progress2 output, e.g.:
#   "9.53G 21% 317.26MB/s 0:00:28 (xfr#83063, to-chk=443926/538653)"
# Group 1: size token (e.g. "9.53G") — used to compute bytes_transferred (WR-01).
# Group 2: percent complete.  Group 3: files transferred so far.  Group 4: total-to-check.
_PROGRESS2_RE = re.compile(r"(\d+[\d.]*[KMGT]?)\s+(\d+)%\s+\S+\s+\S+\s+\(xfr#(\d+),\s*to-chk=\d+/(\d+)\)")

# pc-switcher's own runtime state and installation live under the invoking
# user's home directory. Mirroring them between machines is unsafe:
# sync-history.json is the topology-safety state (ADR-015) and a --delete mirror
# would clobber the target's copy mid-sync; the lock file, logs, and the running
# install must stay machine-local. These paths are ALWAYS excluded from folder
# sync regardless of user config, and are the ONLY hardcoded excludes — every
# other exclusion is user-configurable (ADR-016).
_RUNTIME_EXCLUDE_RELPATHS: tuple[str, ...] = (
    ".local/share/pc-switcher",  # lock, sync-history.json, logs/
    ".local/share/uv/tools/pcswitcher",  # uv tool install (virtualenv)
    ".local/bin/pc-switcher",  # entry-point shim
)


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
    both ends to preserve cross-owner file metadata.

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
        """Convert an rsync progress figure (e.g. '9.53G', '317K', '80.153.795.479') to bytes.

        Two shapes occur in --info=progress2 output:
        - the plain byte counter, which rsync groups with the locale thousands
          separator ('.' under nl_BE, ',' under en_US), e.g. '80.153.795.479';
        - a human-readable figure with a K/M/G/T suffix (only when rsync runs
          with -h), e.g. '9.53G', where the '.' is a decimal point.

        rsync is forced to the C locale in `_build_rsync_cmd` so the counter is
        ungrouped in practice, but this parser stays tolerant of grouping so a
        stray locale-formatted figure can never abort the sync: the byte count
        is best-effort progress metadata (WR-01), not sync-critical.

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
                excludes=f.get("excludes", []),
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
        3. Each active folder path exists on the source.

        Returns the list of validation errors (empty on success).
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

        return errors

    @staticmethod
    def _runtime_exclude_filters(folder_path: str) -> list[str]:
        """rsync `--filter` args protecting pc-switcher's own runtime files.

        The runtime files (`_RUNTIME_EXCLUDE_RELPATHS`) live under the invoking
        user's home directory. When `folder_path` contains that home (e.g. syncing
        `/home`), each protected path is emitted as a root-anchored rsync pattern
        relative to the transfer root, so it matches exactly one location and can
        never be re-exposed by a user include rule. When home is outside
        `folder_path` (e.g. `/root` synced by a normal user) there is nothing to
        protect and the list is empty.
        """
        try:
            rel = Path.home().relative_to(folder_path.rstrip("/") or "/")
        except ValueError:
            return []
        prefix = "/" if str(rel) == "." else f"/{rel}/"
        return [f"--filter={shlex.quote(f'- {prefix}{sub}')}" for sub in _RUNTIME_EXCLUDE_RELPATHS]

    def _build_rsync_cmd(self, folder: FolderEntry, dry_run: bool) -> str:
        """Build the rsync shell command for syncing a single folder.

        Produces a command that:
        - Runs as root on source via `sudo -E rsync` (preserves SSH_AUTH_SOCK
          so the ssh agent socket remains accessible).
        - Elevates to root on target via `--rsync-path='sudo rsync'` over the
          normal-user SSH connection (D-05); root SSH login stays disabled.
        - Uses the D-13 flag baseline: -aAXHS + --numeric-ids + --delete.
        - Adds --dry-run only when `dry_run` is True (D-12).
        - Never includes --delete-excluded (excluded files must survive on target
          — D-06) or --checksum (rsync's built-in verification is trusted — D-14).

        All config-derived values (folder path, exclude patterns, target hostname)
        are shlex.quote'd to prevent shell injection (T-05-01).

        SSH transport note: `sudo -E rsync` runs rsync as root, and the ssh it
        spawns also runs as root.  OpenSSH resolves `~/.ssh` from the running
        uid's passwd entry (root → /root/.ssh) regardless of the $HOME variable.
        Passing explicit credentials (-l, -i, -o UserKnownHostsFile=, -F) with
        the invoking user's paths is the correct fix; relying on $HOME does NOT
        work (empirically verified on target VMs: ssh fails with "Host key
        verification failed" / rsync exit 255 without explicit credentials).
        """
        parts = [
            "sudo",
            "-E",
            # Force the C locale so rsync's --info=progress2 byte counter is
            # printed ungrouped.  Under a grouping locale (e.g. LC_NUMERIC=nl_BE)
            # rsync prints "80.153.795.479"; the progress2 regex captures the
            # whole token and float() then rejects the thousands separators,
            # aborting the sync over a purely cosmetic figure (WR-01).  `env`
            # runs after sudo so it sets the locale for rsync as root regardless
            # of sudo's own env policy.
            "env",
            "LC_ALL=C",
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

        # Build explicit SSH credentials for the -e transport.
        # Background: sudo launches rsync as root; root's ssh resolves ~/.ssh
        # from /root/.ssh (uid lookup), not from $HOME.  Passing the invoking
        # user's assets explicitly is the verified fix (see docstring).
        home = Path.home()
        ssh_dir = home / ".ssh"
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
        parts.append(f"-e {shlex.quote(ssh_cmd)}")

        # Root on target via passwordless sudo scoped to /usr/bin/rsync (D-05).
        parts.append(f"--rsync-path={shlex.quote('sudo rsync')}")

        # Hardcoded protective excludes come FIRST (before user rules) so no user
        # include rule can re-expose pc-switcher's own runtime files (ADR-016).
        parts.extend(self._runtime_exclude_filters(folder.path))

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
                            item=str(folder.path),
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

        # Progress is reported per folder, so the bar resets for each one and would
        # otherwise freeze at the last folder's final percentage (often <100% for a
        # small folder). Mark the whole job complete so the bar reads 100% when done.
        if folders:
            self._report_progress(ProgressUpdate(percent=100))
