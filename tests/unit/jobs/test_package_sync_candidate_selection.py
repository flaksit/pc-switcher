"""Unit tests for the pure candidate-selection/parsing helpers backing the plan 02-13
VM-level `apt_sync` integration test.

These functions have no I/O of their own -- the integration test module
(`tests/integration/jobs/test_package_sync.py`) wires them to real `apt-mark`/
`dpkg-query`/`apt-cache rdepends` output over SSH, but the parsing and selection logic
itself is ordinary Python and gets fast, VM-independent coverage here.
"""

from __future__ import annotations

from tests.integration.jobs.test_package_sync import (
    nonblank_lines,
    parse_batched_rdepends,
    parse_dpkg_installed,
    parse_reverse_depends,
    pick_safe_removal_candidate,
)


class TestNonblankLines:
    def test_strips_and_drops_blank_lines(self) -> None:
        assert nonblank_lines("a\n  b  \n\n c\n") == ["a", "b", "c"]

    def test_empty_input_yields_empty_list(self) -> None:
        assert nonblank_lines("") == []


class TestParseDpkgInstalled:
    def test_only_install_ok_installed_counts(self) -> None:
        output = "pkg-a\tinstall ok installed\npkg-b\tdeinstall ok config-files\npkg-c\tinstall ok installed\n"
        assert parse_dpkg_installed(output) == {"pkg-a", "pkg-c"}

    def test_blank_lines_ignored(self) -> None:
        assert parse_dpkg_installed("\n\npkg-a\tinstall ok installed\n") == {"pkg-a"}

    def test_half_installed_status_excluded(self) -> None:
        assert parse_dpkg_installed("pkg-a\thalf-installed ok half-installed\n") == set()


class TestParseReverseDepends:
    def test_names_after_header_collected(self) -> None:
        block = "mypkg\nReverse Depends:\n  depA\n  depB\n"
        assert parse_reverse_depends(block) == {"depA", "depB"}

    def test_no_header_yields_empty_set(self) -> None:
        assert parse_reverse_depends("mypkg\n") == set()

    def test_takes_first_token_of_each_dependency_line(self) -> None:
        block = "mypkg\nReverse Depends:\n  depA <config-name>\n"
        assert parse_reverse_depends(block) == {"depA"}


class TestParseBatchedRdepends:
    def test_splits_multiple_candidate_blocks(self) -> None:
        output = (
            "@@RDEPENDS_FOR@@pkg-a\npkg-a\nReverse Depends:\n  dep1\n"
            "@@RDEPENDS_FOR@@pkg-b\npkg-b\nReverse Depends:\n  dep2\n  dep3\n"
        )
        assert parse_batched_rdepends(output) == {"pkg-a": {"dep1"}, "pkg-b": {"dep2", "dep3"}}

    def test_candidate_with_no_reverse_deps_maps_to_empty_set(self) -> None:
        output = "@@RDEPENDS_FOR@@pkg-a\npkg-a\n"
        assert parse_batched_rdepends(output) == {"pkg-a": set()}

    def test_empty_input_yields_empty_dict(self) -> None:
        assert parse_batched_rdepends("") == {}


class TestPickSafeRemovalCandidate:
    def test_picks_alphabetically_first_safe_candidate(self) -> None:
        result = pick_safe_removal_candidate(
            pc1_manual=["zeta", "alpha"],
            pc2_installed={"zeta", "alpha"},
            pc2_manual={"zeta", "alpha"},
            reverse_deps_by_candidate={},
        )
        assert result == "alpha"

    def test_skips_candidate_with_manually_installed_reverse_dependency(self) -> None:
        result = pick_safe_removal_candidate(
            pc1_manual=["alpha", "beta"],
            pc2_installed={"alpha", "beta"},
            pc2_manual={"alpha", "beta", "gamma"},
            reverse_deps_by_candidate={"alpha": {"gamma"}},
        )
        assert result == "beta"

    def test_no_intersection_yields_none(self) -> None:
        result = pick_safe_removal_candidate(
            pc1_manual=["alpha"],
            pc2_installed={"beta"},
            pc2_manual=set(),
            reverse_deps_by_candidate={},
        )
        assert result is None

    def test_all_candidates_unsafe_yields_none(self) -> None:
        result = pick_safe_removal_candidate(
            pc1_manual=["alpha"],
            pc2_installed={"alpha"},
            pc2_manual={"alpha", "dep"},
            reverse_deps_by_candidate={"alpha": {"dep"}},
        )
        assert result is None

    def test_reverse_dep_not_manually_installed_is_safe(self) -> None:
        """An installed-but-not-manually-installed reverse dependency does not disqualify
        a candidate -- only a manually-installed one does (the plan's own wording)."""
        result = pick_safe_removal_candidate(
            pc1_manual=["alpha"],
            pc2_installed={"alpha"},
            pc2_manual={"alpha"},
            reverse_deps_by_candidate={"alpha": {"auto-installed-dep"}},
        )
        assert result == "alpha"
