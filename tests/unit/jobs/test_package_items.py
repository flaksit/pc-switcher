"""Unit tests for the item model every package job shares (`packages/items.py`).

Scope is exactly what lives there. A shape or detail string only one manager builds is
tested beside that manager -- `SnapItem` in `test_snap_sync.py`, `AptPinItem` and
`compare_deb_versions` in `test_apt_sync.py` -- so a job's own tests move with the job.
"""

from __future__ import annotations

from pcswitcher.jobs.packages.items import Machines, build_version_mismatch_detail


def test_version_mismatch_detail_contains_both_versions() -> None:
    """D-04 made visible: the review names both versions and proposes nothing, which is
    what "reported, never force-downgraded" looks like in the text the user reads.
    """
    detail = build_version_mismatch_detail("1.0-1", "2.0-1", Machines(source="atlas", target="nomad"))

    assert detail == "atlas has 1.0-1, nomad has 2.0-1"


def test_version_mismatch_detail_names_the_machines_not_their_roles() -> None:
    """The user's ruling: a review line says which of THEIR computers has what. "source"
    and "target" are the tool's words for the two ends of a run, not names of machines."""
    detail = build_version_mismatch_detail("1.0-1", "2.0-1", Machines(source="atlas", target="nomad"))

    assert "source" not in detail
    assert "target" not in detail
