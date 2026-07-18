"""Real-rsync acceptance tests for folder_sync filter rules (issue #166).

These tests need only a local `rsync` binary — no VM, no SSH. Each test shells
out to the real rsync via subprocess against a source/destination tree built
under `tmp_path`, using `-a --delete --dry-run --out-format=%n` plus the
filter args under test, and asserts on the set of transferred relative paths.
Nothing is ever written (--dry-run); tests skip cleanly when rsync is absent.
"""

from __future__ import annotations

import shutil
import subprocess
from importlib.resources import files
from pathlib import Path

import pytest

pytestmark = [
    pytest.mark.local_rsync,
    pytest.mark.skipif(shutil.which("rsync") is None, reason="requires local rsync binary"),
]


def run_rsync(src: Path, dst: Path, *filter_args: str) -> set[str]:
    """Run real rsync in --dry-run mode; return the set of transferred relative paths.

    Trailing slashes (rsync prints them for directory entries) are stripped so
    a directory and a file of the same name compare equal — not a concern for
    these fixtures, but keeps the returned set predictable.
    """
    cmd = [
        "rsync",
        "-a",
        "--delete",
        "--dry-run",
        "--out-format=%n",
        *filter_args,
        f"{src}/",
        f"{dst}/",
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, check=True)
    return {line.rstrip("/") for line in result.stdout.splitlines() if line.strip()}


def _write(path: Path, content: str = "content") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)


class TestCentralMergeCacheIncludeOverride:
    """Acceptance #1: central `merge` filter at a /home-style transfer root.

    Proves floating (non-leading-slash) patterns keep .cache/uv and .cache/pip
    while dropping the rest of .cache, at a transfer root where each user's
    directory sits one level below (alice/.cache/uv, not /.cache/uv) — the
    FLAGGED PLANNER DECISION in the plan's CONTEXT.
    """

    def test_shipped_home_filter_keeps_dev_caches_drops_rest(self, tmp_path: Path) -> None:
        src = tmp_path / "src"
        dst = tmp_path / "dst"
        _write(src / "alice/.cache/uv/pkg1")
        _write(src / "alice/.cache/pip/pkg2")
        _write(src / "alice/.cache/nvidia/x")
        _write(src / "alice/.cache/fontconfig/y")
        _write(src / "alice/.ssh/id_rsa")
        _write(src / "alice/docs/keep.txt")
        dst.mkdir()

        # Track the real shipped home.filter, not a hand-copied fixture.
        filter_file = tmp_path / "home.filter"
        filter_file.write_text(files("pcswitcher").joinpath("home.filter").read_text())

        transferred = run_rsync(src, dst, f"--filter=merge {filter_file}")

        assert "alice/.cache/uv/pkg1" in transferred
        assert "alice/.cache/pip/pkg2" in transferred
        assert "alice/.cache/nvidia/x" not in transferred
        assert "alice/.cache/fontconfig/y" not in transferred
        assert "alice/.ssh/id_rsa" not in transferred
        assert "alice/docs/keep.txt" in transferred


class TestPerDirDirMerge:
    """Acceptance #2: `.pcswitcher-filter` inherits into subdirs and self-transfers."""

    def test_dir_merge_inherits_and_transfers_itself(self, tmp_path: Path) -> None:
        src = tmp_path / "src"
        dst = tmp_path / "dst"
        _write(src / "proj/.pcswitcher-filter", "- secret.env\n")
        _write(src / "proj/secret.env")
        _write(src / "proj/keep.txt")
        _write(src / "proj/sub/secret.env")
        _write(src / "proj/sub/keep.txt")
        dst.mkdir()

        transferred = run_rsync(src, dst, "--filter=dir-merge /.pcswitcher-filter")

        assert "proj/secret.env" not in transferred
        assert "proj/sub/secret.env" not in transferred  # inherits into subdirectory
        assert "proj/keep.txt" in transferred
        assert "proj/sub/keep.txt" in transferred
        assert "proj/.pcswitcher-filter" in transferred  # no `e` modifier: file itself transfers


class TestHostileRsyncFilterNoOp:
    """Acceptance #3: a planted `.rsync-filter` has zero effect (no -F/-FF/-C)."""

    def test_hostile_rsync_filter_is_inert(self, tmp_path: Path) -> None:
        src = tmp_path / "src"
        dst = tmp_path / "dst"
        _write(src / "proj/.rsync-filter", "- main.py\n")
        _write(src / "proj/main.py")
        dst.mkdir()

        # Only the job's own per-dir arg is passed — never -F/-FF/-C/--cvs-exclude.
        transferred = run_rsync(src, dst, "--filter=dir-merge /.pcswitcher-filter")

        assert "proj/main.py" in transferred
        assert "proj/.rsync-filter" in transferred  # transfers as ordinary content, not activated


class TestGlobalFirstEnforcement:
    """Backs the un-overridable-rules property: central `-` wins over per-dir `+`."""

    def test_central_exclude_cannot_be_reexposed_by_per_dir_include(self, tmp_path: Path) -> None:
        src = tmp_path / "src"
        dst = tmp_path / "dst"
        central = tmp_path / "central.filter"
        central.write_text("- secret.env\n")
        _write(src / "proj/.pcswitcher-filter", "+ secret.env\n")
        _write(src / "proj/secret.env")
        dst.mkdir()

        # merge BEFORE dir-merge, mirroring _build_rsync_cmd's GLOBAL-FIRST order.
        transferred = run_rsync(
            src,
            dst,
            f"--filter=merge {central}",
            "--filter=dir-merge /.pcswitcher-filter",
        )

        assert "proj/secret.env" not in transferred


class TestPreseedProtectsTargetOnFirstSync:
    """The filter pre-seed pass makes a per-directory exclude protect a pre-existing target
    file on the FIRST sync — when the target does not yet have the `.pcswitcher-filter`.

    A dir-merge rule is read per-side, so without the filter file on the receiver, the
    main `--delete` mirror deletes (not protects) the target file the rule names. folder_sync
    runs a preliminary pass (execute() -> _build_preseed_cmd) that copies only the
    `.pcswitcher-filter` files to the target first; these tests reproduce both commands with
    real rsync (no --dry-run, so deletions actually happen) and pin both the bug and the fix.
    """

    @staticmethod
    def _seed(tmp_path: Path) -> tuple[Path, Path]:
        src = tmp_path / "src"
        dst = tmp_path / "dst"
        _write(src / "proj/.pcswitcher-filter", "- secret.env\n")  # rule lives on the source
        _write(src / "proj/keep.txt", "keep-src")
        _write(dst / "proj/secret.env", "tgt-only-secret")  # target-only file the rule names
        return src, dst

    @staticmethod
    def _mirror(src: Path, dst: Path) -> None:
        # Mirrors _build_rsync_cmd: the real --delete pass with dir-merge.
        subprocess.run(
            ["rsync", "-a", "--delete", "--filter=dir-merge /.pcswitcher-filter", f"{src}/", f"{dst}/"],
            check=True,
            capture_output=True,
            text=True,
        )

    def test_single_pass_deletes_the_excluded_target_file(self, tmp_path: Path) -> None:
        """Without pre-seeding, the main mirror DELETES the rule-excluded target file (the bug)."""
        src, dst = self._seed(tmp_path)
        self._mirror(src, dst)
        assert not (dst / "proj/secret.env").exists()

    def test_preseed_then_mirror_protects_the_excluded_target_file(self, tmp_path: Path) -> None:
        """Pre-seeding the .pcswitcher-filter first makes the main mirror PROTECT the target file."""
        src, dst = self._seed(tmp_path)
        # Pass 1 (mirrors _build_preseed_cmd): seed only .pcswitcher-filter files, NO --delete.
        subprocess.run(
            ["rsync", "-a", "--filter=+ */", "--filter=+ .pcswitcher-filter", "--filter=- *", f"{src}/", f"{dst}/"],
            check=True,
            capture_output=True,
            text=True,
        )
        # Pass 2: the real --delete mirror.
        self._mirror(src, dst)

        assert (dst / "proj/secret.env").read_text() == "tgt-only-secret"  # protected, not deleted
        assert (dst / "proj/keep.txt").read_text() == "keep-src"  # non-excluded file still syncs
        assert (dst / "proj/.pcswitcher-filter").exists()  # the filter file itself transferred
