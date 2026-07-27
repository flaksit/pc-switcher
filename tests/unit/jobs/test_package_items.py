"""Unit tests for the item model every package job shares (`packages/items.py`).

Scope is exactly what lives there. A shape or detail string only one manager builds is
tested beside that manager -- `SnapItem` in `test_snap_sync.py`, `AptPinItem` and
`compare_deb_versions` in `test_apt_sync.py` -- so a job's own tests move with the job.
"""

from __future__ import annotations

from pcswitcher.jobs.packages.items import build_version_mismatch_detail


def test_version_mismatch_detail_contains_both_versions() -> None:
    """D-04 made visible: the review names both versions and proposes nothing, which is
    what "reported, never force-downgraded" looks like in the text the user reads.
    """
    detail = build_version_mismatch_detail("1.0-1", "2.0-1")

    assert "1.0-1" in detail
    assert "2.0-1" in detail
