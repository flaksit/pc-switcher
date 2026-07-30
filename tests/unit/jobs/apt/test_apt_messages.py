"""The sentences the review shows, asserted as text because that is what the user acts on.

Split out of the former single `test_apt_sync.py`.
"""

from __future__ import annotations

from pcswitcher.jobs.apt_sync.messages import (
    build_origin_detail,
    build_origin_mismatch_detail,
    build_origin_refusal_detail,
    build_repo_removal_detail,
    build_repo_unavailable_detail,
)
from tests.unit.jobs.apt.helpers import (
    MACHINES,
)


class TestOriginDetailWording:
    """Ruling 9's naming rules, as pure text."""

    def test_origin_detail_strips_the_scheme_and_names_the_full_path(self) -> None:
        """The path, not the bare host: one Launchpad host serves thousands of PPAs."""
        assert build_origin_detail(["https://ppa.launchpadcontent.net/git-core/ppa/ubuntu"]) == (
            "from ppa.launchpadcontent.net/git-core/ppa/ubuntu"
        )

    def test_origin_detail_is_omitted_for_a_distribution_origin(self) -> None:
        """The caller filters the distribution's own origins out, so nothing left to name
        means the distribution serves it and the line says nothing about origins.
        """
        assert build_origin_detail([]) is None

    def test_several_vendors_are_named_comma_separated(self) -> None:
        assert build_origin_detail(["https://a.example.com/apt", "https://b.example.com/deb"]) == (
            "from a.example.com/apt, b.example.com/deb"
        )

    def test_the_mismatch_detail_names_both_sides(self) -> None:
        detail = build_origin_mismatch_detail(
            ["https://vendor.example.com/apt"], ["https://rival.example.com/apt"], MACHINES
        )

        assert detail == (
            "source-host installed it from vendor.example.com/apt, target-host from rival.example.com/apt"
        )


class TestOriginRefusalWording:
    """The refusal names both origins, because either half alone is unactionable."""

    def test_both_the_wanted_and_the_offered_origin_are_named(self) -> None:
        detail = build_origin_refusal_detail(
            "firefox", ["https://packages.mozilla.org/apt"], ["http://ftp.belnet.be/ubuntu"], MACHINES
        )

        assert detail == (
            "firefox was not installed: source-host has it from packages.mozilla.org/apt, but after this run's "
            "apt-get update target-host would install it from ftp.belnet.be/ubuntu (ADR-020 D-35)"
        )

    def test_a_target_with_no_candidate_origin_says_so_rather_than_naming_nothing(self) -> None:
        detail = build_origin_refusal_detail("pkg-a", ["https://vendor.example.com/apt"], [], MACHINES)

        assert "offers it from no repository at all" in detail
        assert "vendor.example.com/apt" in detail


class TestRepoUnavailableWording:
    """`REPO_UNAVAILABLE`'s detail: the source's origin cannot be provided (ADR-020 D-25)."""

    def test_build_repo_unavailable_detail_names_the_package_its_origin_and_the_cause(self) -> None:
        detail = build_repo_unavailable_detail(
            "brscan3", ["https://gone.example.com/apt"], "no repository file on atlas declares it", MACHINES
        )

        assert detail == (
            "target-host cannot install brscan3 from gone.example.com/apt: no repository file on atlas declares it"
        )


class TestRepoRemovalWording:
    """A repository deletion is decided from the URLs it serves, not from its filename —
    which is whatever whoever created the file happened to call it."""

    def test_the_urls_are_the_whole_detail(self) -> None:
        """Nothing about stranded software: a file still feeding anything the target keeps
        never reaches this text (`PKG-FR-REPO-DELETE`)."""
        detail = build_repo_removal_detail(["https://cli.github.com/packages"], MACHINES)

        assert detail == "target-host would stop getting software from https://cli.github.com/packages"

    def test_every_url_the_file_declares_is_named(self) -> None:
        detail = build_repo_removal_detail(["https://a.example.com/apt", "https://b.example.com/deb"], MACHINES)

        assert detail == (
            "target-host would stop getting software from https://a.example.com/apt, https://b.example.com/deb"
        )

    def test_a_file_declaring_no_url_says_so_rather_than_trailing_off(self) -> None:
        """A commented-out leftover parses to no URI. Half a sentence would read as a bug."""
        detail = build_repo_removal_detail([], MACHINES)

        assert detail == "target-host would stop getting software from nowhere — it declares no repository URL"
