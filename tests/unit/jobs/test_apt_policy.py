"""Unit tests for `packages/apt_policy.py` — the shared `apt-cache policy` version-table
walk both package jobs read their origin facts out of.

Every fixture below is real `apt-cache policy` output, captured from a live Ubuntu 24.04
machine. Where a shape does not exist on that machine (an installed version and a candidate
from two DIFFERENT vendors) the block is composed from that machine's own verbatim rows
with only the `Candidate:` value changed, and says so — a hand-invented version table would
test the parser against a format nobody has ever seen apt emit.
"""

from __future__ import annotations

from pcswitcher.jobs.packages.apt_policy import (
    candidate_origins_by_package,
    installed_origins_by_package,
    normalise_repo_uri,
)

# `firefox`, not installed: the archive's only candidate carries epoch 1, which is the fact
# ADR-021 D-36 rests on. Verbatim.
POLICY_ARCHIVE_CANDIDATE_UNINSTALLED = """firefox:
  Installed: (none)
  Candidate: 1:1snap1-0ubuntu5
  Version table:
     1:1snap1-0ubuntu5 500
        500 http://ftp.belnet.be/ubuntu noble/main amd64 Packages
"""

# `git`, installed from a PPA that also outranks the archive's own copy. Verbatim, and the
# source of ruling 9's full-URI-path example.
POLICY_PPA_INSTALLED = """git:
  Installed: 1:2.54.0-0ppa1~ubuntu24.04.1
  Candidate: 1:2.54.0-0ppa1~ubuntu24.04.1
  Version table:
 *** 1:2.54.0-0ppa1~ubuntu24.04.1 500
        500 https://ppa.launchpadcontent.net/git-core/ppa/ubuntu noble/main amd64 Packages
        100 /var/lib/dpkg/status
     1:2.43.0-1ubuntu7.3 500
        500 http://ftp.belnet.be/ubuntu noble-updates/main amd64 Packages
        500 http://security.ubuntu.com/ubuntu noble-security/main amd64 Packages
     1:2.43.0-1ubuntu7 500
        500 http://ftp.belnet.be/ubuntu noble/main amd64 Packages
"""

# COMPOSED, not measured: `gh`'s real block with its `Candidate:` pointed at the ESM row
# instead of the installed one. Every version row, priority and URI is verbatim from the
# development machine; only the one `Candidate:` value differs, because no package on that
# machine currently has its installed and candidate versions from two different vendors —
# which is exactly the shape the Firefox defect takes.
POLICY_INSTALLED_AND_CANDIDATE_DIFFER = """gh:
  Installed: 2.96.0
  Candidate: 2.45.0-1ubuntu0.3+esm3
  Version table:
 *** 2.96.0 1001
        500 https://cli.github.com/packages stable/main amd64 Packages
        100 /var/lib/dpkg/status
     2.45.0-1ubuntu0.3+esm3 510
        510 https://esm.ubuntu.com/apps/ubuntu noble-apps-security/main amd64 Packages
     2.45.0-1ubuntu0.3 500
        500 http://ftp.belnet.be/ubuntu noble-updates/universe amd64 Packages
        500 http://security.ubuntu.com/ubuntu noble-security/universe amd64 Packages
     2.45.0-1build1 500
        500 http://ftp.belnet.be/ubuntu noble/universe amd64 Packages
"""

# `docker.io`, fully repo-available but pinned below zero. Verbatim.
POLICY_NO_CANDIDATE = """docker.io:
  Installed: 29.1.3-0ubuntu3~24.04.2
  Candidate: (none)
  Version table:
 *** 29.1.3-0ubuntu3~24.04.2 -1
        510 https://esm.ubuntu.com/apps/ubuntu noble-apps-security/main amd64 Packages
        500 http://ftp.belnet.be/ubuntu noble-updates/universe amd64 Packages
        500 http://security.ubuntu.com/ubuntu noble-security/universe amd64 Packages
        100 /var/lib/dpkg/status
     24.0.7-0ubuntu4 500
        500 http://ftp.belnet.be/ubuntu noble/universe amd64 Packages
"""

# COMPOSED, not measured: the development machine does not have Mozilla's repository, so
# this is `firefox`'s real archive row (above, verbatim) with a vendor row added in the shape
# `git`'s PPA row has, at the priority Mozilla's documented `preferences.d` pin gives it. It
# is the source-side half of the defect ADR-021 D-34 closes: the vendor's copy is installed
# here, and the archive's epoch-1 copy is what the target would install.
POLICY_MOZILLA_FIREFOX_INSTALLED = """firefox:
  Installed: 145.0
  Candidate: 145.0
  Version table:
 *** 145.0 1000
        1000 https://packages.mozilla.org/apt mozilla/main amd64 Packages
        100 /var/lib/dpkg/status
     1:1snap1-0ubuntu5 500
        500 http://ftp.belnet.be/ubuntu noble/main amd64 Packages
"""

# `code`, installed from a downloaded `.deb`: dpkg's own status entry supplies the
# candidate, so the candidate row has no repository origin at all. Verbatim.
POLICY_HAND_DEB = """code:
  Installed: 1.129.1-1784303641
  Candidate: 1.129.1-1784303641
  Version table:
 *** 1.129.1-1784303641 100
        100 /var/lib/dpkg/status
"""


class TestCandidateOrigins:
    """`candidate_origins_by_package` reads the CANDIDATE row, which is a different row
    from the one `installed_origins_by_package` reads and answers a different question.
    """

    def test_candidate_origins_come_from_the_candidate_row_not_the_installed_one(self) -> None:
        """The defect origin classification exists to catch: vendor A's copy is installed,
        vendor B's is what apt would install. Matching on name alone reads the second as
        "the target can already supply this".
        """
        candidate = candidate_origins_by_package(POLICY_INSTALLED_AND_CANDIDATE_DIFFER)
        installed = installed_origins_by_package(POLICY_INSTALLED_AND_CANDIDATE_DIFFER)

        assert candidate["gh"] == frozenset({"https://esm.ubuntu.com/apps/ubuntu"})
        assert installed["gh"] == frozenset({"https://cli.github.com/packages"})

    def test_a_name_apt_printed_no_block_for_reaches_no_key(self) -> None:
        """`df48cd07`'s rule: apt's silence is not evidence. Verified against a live
        `apt-cache policy <unknown-name> brscan3`, which printed one block, not two.
        """
        result = candidate_origins_by_package(POLICY_PPA_INSTALLED)

        assert "git" in result
        assert "firefox" not in result

    def test_candidate_none_yields_an_empty_origin_set_not_a_missing_key(self) -> None:
        """The distinction the "no target candidate" classes turn on: apt knows the name
        and will install nothing, versus apt was never asked or never answered.
        """
        result = candidate_origins_by_package(POLICY_NO_CANDIDATE)

        assert result["docker.io"] == frozenset()

    def test_a_candidate_for_an_uninstalled_package_is_read_from_a_table_with_no_installed_row(
        self,
    ) -> None:
        """The whole point of the candidate row: the package is not installed, so there is
        no `***` marker to key off at all.
        """
        result = candidate_origins_by_package(POLICY_ARCHIVE_CANDIDATE_UNINSTALLED)

        assert result["firefox"] == frozenset({"http://ftp.belnet.be/ubuntu"})

    def test_a_candidate_that_is_the_installed_version_reads_that_rows_origins(self) -> None:
        """The ordinary case, where the two questions happen to have one answer — and the
        `/var/lib/dpkg/status` pseudo-origin on that row is still not a repository.
        """
        result = candidate_origins_by_package(POLICY_PPA_INSTALLED)

        assert result["git"] == frozenset({"https://ppa.launchpadcontent.net/git-core/ppa/ubuntu"})

    def test_a_candidate_supplied_only_by_dpkg_has_an_empty_origin_set(self) -> None:
        """A hand-installed `.deb`: apt reports a candidate because dpkg's status entry
        supplies one, and no repository can deliver it.
        """
        result = candidate_origins_by_package(POLICY_HAND_DEB)

        assert result["code"] == frozenset()

    def test_several_blocks_in_one_batched_run_stay_separate(self) -> None:
        """One command, many names — the only shape either job ever issues."""
        result = candidate_origins_by_package(
            POLICY_PPA_INSTALLED + POLICY_ARCHIVE_CANDIDATE_UNINSTALLED + POLICY_NO_CANDIDATE
        )

        assert result["git"] == frozenset({"https://ppa.launchpadcontent.net/git-core/ppa/ubuntu"})
        assert result["firefox"] == frozenset({"http://ftp.belnet.be/ubuntu"})
        assert result["docker.io"] == frozenset()


class TestInstalledOriginsUnderTheSharedWalk:
    """The installed-row answers that must not move now that both parsers share one walk."""

    def test_only_the_installed_rows_origins_count(self) -> None:
        """`git`'s older version rows name three archive URIs that are emphatically not
        where the installed version came from.
        """
        assert installed_origins_by_package(POLICY_PPA_INSTALLED)["git"] == frozenset(
            {"https://ppa.launchpadcontent.net/git-core/ppa/ubuntu"}
        )

    def test_an_uninstalled_package_reaches_an_empty_installed_set(self) -> None:
        assert installed_origins_by_package(POLICY_ARCHIVE_CANDIDATE_UNINSTALLED)["firefox"] == frozenset()

    def test_a_pinned_out_package_still_reports_its_installed_origins(self) -> None:
        """`Candidate: (none)` says nothing about where the installed copy came from."""
        assert installed_origins_by_package(POLICY_NO_CANDIDATE)["docker.io"] == frozenset(
            {
                "https://esm.ubuntu.com/apps/ubuntu",
                "http://ftp.belnet.be/ubuntu",
                "http://security.ubuntu.com/ubuntu",
            }
        )


class TestNormaliseRepoUri:
    def test_the_trailing_slash_apt_strips_is_stripped(self) -> None:
        assert normalise_repo_uri("https://packages.microsoft.com/repos/azure-cli/") == (
            "https://packages.microsoft.com/repos/azure-cli"
        )
