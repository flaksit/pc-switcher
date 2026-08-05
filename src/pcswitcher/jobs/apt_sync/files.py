"""Every file this job puts on, or takes off, the target — and the only module that knows
how a root-owned `/etc/apt` file is written at all.

`RemoteExecutor.send_file` is plain SFTP as the ordinary SSH user with no sudo path
(`executor.py:508`) and cannot write into `/etc/apt` directly, so every write is the same
two-step: bytes land under the target user's own `~/.cache` staging directory, then `sudo
install` promotes them with the right ownership and mode. Keeping that in one place is what
lets the repository unit and the keyring provisioning share it without either owning it, and
what keeps shell strings out of both.
"""

from __future__ import annotations

import shlex
from pathlib import Path

from pcswitcher.executor import RemoteExecutor
from pcswitcher.jobs.packages.sync_core import ConvergeItemFailed
from pcswitcher.models import CommandResult


def backup_path_for(backup_dir: str, dest: str) -> str:
    """A stable, unique backup filename for an absolute `dest` path, flattened into
    `backup_dir` (`/etc/apt/sources.list.d/foo.list` -> `etc_apt_sources.list.d_foo.list`)
    so every backed-up file lives directly under one run-scoped directory.
    """
    return f"{backup_dir}/{dest.lstrip('/').replace('/', '_')}"


def staged_name_for(dest: str) -> str:
    """The staging filename an absolute destination gets, flattened the same way a backup
    is so two destinations can never collide in the one staging directory."""
    return dest.lstrip("/").replace("/", "_")


class TargetFiles:
    """The target's filesystem, for the paths this job owns."""

    def __init__(self, target: RemoteExecutor) -> None:
        self._target = target
        self._home: str | None = None

    async def home(self) -> str:
        """The target user's home directory, resolved once per run via `echo $HOME`
        (`config_sync._copy_config_to_target`'s established pattern) and cached — every
        repository-group file write needs the same absolute staging path.
        """
        if self._home is None:
            result = await self._target.run_command("echo $HOME", login_shell=False)
            self._home = result.stdout.strip()
        return self._home

    async def staging_dir(self) -> str:
        """The run's staging directory, created if absent."""
        staging_dir = f"{await self.home()}/.cache/pc-switcher/apt-staging"
        await self._target.run_command(
            f"mkdir --parents {shlex.quote(staging_dir)}",
            login_shell=False,
            mutates="create the apt repository-group staging directory",
        )
        return staging_dir

    async def backup(self, dest: str, backup_dir: str) -> bool:
        """Back up `dest` into `backup_dir` if it currently exists on the target;
        returns whether it existed (so rollback knows restore-vs-delete per file).
        """
        quoted_dest = shlex.quote(dest)
        exists = await self._target.run_command(f"test -f {quoted_dest}", login_shell=False)
        if not exists.success:
            return False

        await self._target.run_command(
            f"mkdir --parents {shlex.quote(backup_dir)}",
            login_shell=False,
            mutates="create the repository-group backup directory",
        )
        backup_path = backup_path_for(backup_dir, dest)
        result = await self._target.run_command(
            f"sudo cp --archive {quoted_dest} {shlex.quote(backup_path)}",
            login_shell=False,
            mutates=f"back up {dest} before the repository group is written",
        )
        if not result.success:
            raise ConvergeItemFailed(
                f"failed to back up {dest} before converging the repository group: {result.stderr.strip()}"
            )
        return True

    async def restore(self, dest: str, backup_dir: str) -> CommandResult:
        """Put `dest` back from its backup copy."""
        return await self._target.run_command(
            f"sudo install --owner=root --group=root --mode=0644 "
            f"{shlex.quote(backup_path_for(backup_dir, dest))} {shlex.quote(dest)}",
            login_shell=False,
            mutates=f"ROLLBACK: restore {dest} from backup",
        )

    async def delete(self, dest: str, *, mutates: str) -> CommandResult:
        return await self._target.run_command(
            f"sudo rm --force {shlex.quote(dest)}", login_shell=False, mutates=mutates
        )

    async def discard_backup(self, backup_dir: str, *, mutates: str) -> CommandResult:
        return await self._target.run_command(
            f"rm --recursive --force {shlex.quote(backup_dir)}", login_shell=False, mutates=mutates
        )

    async def stage_and_promote(self, local: str, dest: str, staging_dir: str, staged_name: str) -> None:
        """Copy the SOURCE machine's `local` onto the target at `dest`, byte-for-byte
        (T-02-35). The two paths are the same for every `/etc/apt` file this job writes
        except a keyring the two machines keep in different directories, where the
        destination has to be the path the repository's `Signed-By:` actually names.

        Bytes land under the target user's own `~/.cache` staging directory first, then `sudo
        install` promotes them with the right ownership/mode in one atomic step (no window
        where the file exists under `/etc/apt` owned by the wrong user, unlike a `mv` plus
        separate `chown`/`chmod`). The staging copy is removed in a `finally` so a failed
        promotion never leaves transferred key material sitting in the cache.
        """
        # `sources.list.d`, `preferences.d`, `apt.conf.d` and `trusted.gpg.d` ship with
        # the `apt` package, but `/etc/apt/keyrings` is a third-party convention that a
        # fresh Ubuntu 24.04 target does not have — `install` (unlike `install -D`)
        # never creates DEST's missing parent directories, so a per-repo key promotion
        # to a fresh machine would otherwise fail every time. `mkdir --parents --mode` only chmods
        # directories it actually creates (unlike `install --directory`, which would also chmod
        # the four directories that already exist), so this is a no-op everywhere except
        # the one directory this project actually needs to create.
        dest_dir = str(Path(dest).parent)
        mkdir_result = await self._target.run_command(
            f"sudo mkdir --parents --mode=0755 {shlex.quote(dest_dir)}",
            login_shell=False,
            mutates=f"create directory {dest_dir} for {dest}",
        )
        if not mkdir_result.success:
            raise ConvergeItemFailed(
                f"failed to prepare directory {dest_dir} for {dest}: {mkdir_result.stderr.strip()}"
            )

        staged_dest = f"{staging_dir}/{staged_name}"
        try:
            await self._target.send_file(
                Path(local), staged_dest, mutates=f"stage {dest} into {staging_dir} before promotion"
            )
            promote = await self._target.run_command(
                f"sudo install --owner=root --group=root --mode=0644 {shlex.quote(staged_dest)} {shlex.quote(dest)}",
                login_shell=False,
                mutates=f"promote the staged file into {dest} as root:root 0644",
            )
            if not promote.success:
                raise ConvergeItemFailed(f"failed to install {dest}: {promote.stderr.strip()}")
        finally:
            await self._target.run_command(
                f"rm --force {shlex.quote(staged_dest)}",
                login_shell=False,
                mutates=f"remove the staging copy of {dest}",
            )
