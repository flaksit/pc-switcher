"""Unit tests for the pure output parsers backing the VM-level package-sync integration tests.

These functions have no I/O of their own -- the integration scenario module
(`tests/integration/jobs/package_sync_scenario.py`) wires them to real `apt-mark`/
`dpkg-query` output over SSH, but the parsing itself is ordinary Python and gets fast,
VM-independent coverage here.
"""

from __future__ import annotations

from tests.integration.jobs.package_sync_scenario import nonblank_lines, parse_dpkg_installed


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
