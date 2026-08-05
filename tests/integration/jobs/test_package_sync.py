"""Integration tests proving the tracer's end-to-end apt_sync path against real VMs.

`apt_sync` (plan 02-03) claims that a package missing on the target travels source
capture -> target query -> diff -> apt_sync's own batched review (each manager reviews
its own diffs inside its own `execute()`, per the corrected D-24; there is no
cross-manager coordinator) -> `apt-get install` on the target. Plan 02-03's own unit
tests only prove that shape against a mocked executor; this module is the VM-level proof
against real apt/dpkg/sudo.

The tests drive each manager's review non-interactively through
`PCSWITCHER_PACKAGE_REVIEW_AUTOMATION` (D-26's hidden test hook,
`jobs.packages.review`) rather than through a real TTY, and assert against the target's
own package-manager or filesystem state (`apt-mark showmanual`, `/etc/apt`, `snap list`,
the pushed snippet registry) -- never against pc-switcher's log text -- except where an
explicit witness legitimately needs the run's own output: the flatpak ORIGIN_MISMATCH
finding, because a REPORT_ONLY diff changes nothing anywhere, and the non-interactive
skip-all claims, whose subject includes what a run with nobody to ask must SAY. Those read
the output through
`collapse_run_output`, which is where the wrapping every Rich renderer applies is dealt
with once, and each matches a whole phrase the code owns rather than a bare name: the log
records every command's own output verbatim at DEBUG (`PKG-FR-LOG-VERBATIM`) and every
config here sets `tui: DEBUG`, so a filename appearing SOMEWHERE in a run is no evidence
of anything. `apt-cache rdepends` output is also read to pick a safe removal candidate
before either machine's package state is touched.

The classes below are SCENARIOS, not one claim each. A `pc-switcher sync` costs 30-40s of
wall clock whatever it converges, so the number of runs is the whole cost of this module
(#216) -- and claims that want the same shape of run share one. Each class states which
premise its claims share, and each test's docstring lists every contract id it settles, so
what is proven stays traceable to
docs/dev/package-sync-scenario-coverage.md. Three shapes recur:

- a run with nobody to ask, which applies nothing and is the only run that PRINTS every
  review group (`TestARunWithNobodyToAsk`);
- runs whose subject is a removal, which only a real `apt-get remove` or `snap remove` on a
  real machine can witness (`TestWhatARealRemovalTakesWithIt`,
  `TestSkipAlwaysIsInertInBothRoles`);
- runs that FAIL, ABORT or are KILLED, which no converging run can carry and which
  therefore keep a sync each.

The plainly converging run -- one seeded divergence per manager, rehearsed, converged and
re-run to a fixed point -- is not here: it rides on the syncs `test_end_to_end_sync.py` and
`test_dry_run.py` already make.

Every helper these tests seed, converge and assert with lives in
`package_sync_scenario.py`. Its pure parsing helpers (`nonblank_lines`,
`parse_dpkg_installed`) have no I/O of their own and are unit-tested directly in
`tests/unit/jobs/test_package_sync_candidate_selection.py`, independent of VM access.

Subjects: every test here needs a package it may hold, diverge, remove and reinstall, and
a stock Ubuntu 24.04 VM offers none for snap (only `SNAP_REMOVAL_DENYLIST` members), none
at all for flatpak (which is not installed), and no apt package that is not one the machine
or pc-switcher itself needs. Those subjects are therefore CREATED --
`tests/integration/scripts/internal/vm-test-fixtures.sh`, baked into the baseline snapshot
by provisioning and re-applied by the module-scoped `vm_test_fixtures` fixture. No test in
this module declines to run for want of a subject: a missing subject is a broken machine
and fails naming what is missing and which script creates it. Which apt package plays which
role is pinned in `FIXTURE_APT_SUBJECTS` and handed out once per module by `apt_subjects`,
which only verifies both machines carry them.

Preconditions, not teardown: a test states the package state it needs and converges to it
(`ensure_absent`, `ensure_installed_and_manual` for apt, `ensure_snaps_installed` behind
`snap_subjects`/`holdable_snaps` for snap) instead of putting the machines back afterwards.
What one scenario leaves behind is usually what the next one wanted anyway, so the converger
reads and returns. Nobody restores the packages at the end either:
`run-integration-tests.sh` replaces both VMs' subvolumes with their baseline btrfs
snapshots and reboots before every run, which is what makes the machines identical run to
run -- so a package left removed costs nothing and undoing it would.

Cleanup that costs nothing -- `/etc/apt` files, markers, holds, `refresh.hold`, paths taken
aside -- stays in each test's `finally`, and the `/etc/apt` half has to: a synthetic
repository left configured makes every later `apt-get update` on that machine slower and
noisier for the rest of the run. What a test INSTALLED is left installed. Every package built
or fetched here is uuid-suffixed or comes from a repository the same test declared, so no
later selection or assertion reaches it and the review lines it may raise are left unapproved
by the automation hook's SKIP_ONCE default -- an `apt-get purge` would spend seconds of dpkg
work undoing what the next run's baseline reset undoes anyway.

The flatpak subject is the REAL Flathub, and its app is provisioned on pc1 only, so the
source->target divergence a converging run needs is part of the baseline rather than
something a test manufactures. A locally built stand-in repository would only ever test
this project's model of a remote; #215's key replication is about a real remote's real
trust configuration (`FIXTURE_FLATPAK_APP`).
"""

from __future__ import annotations

import asyncio
import re
import shlex
from uuid import uuid4

import pytest

from pcswitcher.executor import BashLoginRemoteExecutor
from pcswitcher.jobs.apt_sync.items import AptPackageItem
from pcswitcher.jobs.packages.review import Decision
from pcswitcher.jobs.packages.state import (
    DecisionFile,
    SnippetRegistry,
)
from pcswitcher.models import CommandResult
from tests.integration import SKIP_INSTALL_ON_TARGET
from tests.integration.jobs.package_sync_scenario import (
    APT_KEYRINGS_DIR,
    APT_PREFERENCES_DIR,
    APT_SOURCES_DIR,
    CONTINUE_TEST_MARKER_FAIL,
    CONTINUE_TEST_MARKER_INSTALL_FIRST,
    CONTINUE_TEST_MARKER_INSTALL_SECOND,
    CONTINUE_TEST_MARKERS,
    DELIBERATE_FAILURE_MESSAGE,
    ESM_SOURCE_BODIES,
    FIXTURE_UNUSED_FLATPAK_REMOTE,
    HOLD_POLL_INTERVAL_SECONDS,
    HOLD_POLL_TIMEOUT_SECONDS,
    KILL_RUNNING_SYNC_CMD,
    REGISTRY_DIR_RELPATH,
    SNAP_HOLD_DURATION_SLACK,
    SNAP_HOLD_EXPECTED_DURATION,
    SNAP_STORE_OFFLINE_CMD,
    SNAP_STORE_ONLINE_CMD,
    STOCK_DIRECTORIES,
    SYNTHETIC_PACKAGE_VERSION,
    SYNTHETIC_REPO_HOST,
    UNASKED_ITEM_MARKER,
    VENDOR_PACKAGE,
    VENDOR_REPO_URI,
    AptSubjects,
    a_name_apt_knows_the_machine_does_not_have,
    apt_get_update,
    apt_update_lines_naming,
    author_snippet,
    automation_env_assignment,
    automation_env_assignment_multi,
    capture_machine_package_state,
    capture_system_refresh_hold,
    cleanup_in_parallel,
    collapse_run_output,
    collateral_removal_item_id,
    create_extra_on_target_apt_package,
    create_sideloaded_snap,
    create_synthetic_pin,
    create_synthetic_repo_and_key,
    create_unowned_marker,
    decision_file_exists,
    engage_system_refresh_hold,
    ensure_absent,
    ensure_installed_and_manual,
    finish_both,
    flatpak_app_rows,
    flatpak_remote_row,
    flatpak_subject,
    folder_sync_section,
    holdable_snaps,
    home_dir,
    install_a_hand_downloaded_deb,
    install_from_a_repo_the_target_lacks,
    install_from_the_vendor_repository,
    installed_base_snap,
    machine_utc_now,
    no_candidate_item_id,
    nonblank_lines,
    parse_dpkg_installed,
    parse_rfc3339_utc,
    parse_snap_list_names_revisions,
    publish_a_cascading_pair,
    publish_a_rival_candidate,
    put_paths_back,
    remove_sideloaded_snap,
    remove_the_rival_candidate,
    remove_unowned_marker,
    restore_auto_marked_package,
    restore_flatpak_target_baseline,
    restore_system_refresh_hold,
    snap_notes,
    snap_revision,
    snap_saved_rows,
    snap_subjects,
    take_paths_aside,
    undeclare_local_repository,
    undeclare_the_vendor_repository,
    unowned_item_id,
    write_apt_sync_config,
    write_package_sync_config,
)

pytestmark = pytest.mark.area_package


@pytest.fixture(scope="module", autouse=True)
def _package_sync_subjects(package_sync_subjects: None) -> None:  # pyright: ignore[reportUnusedFunction]
    """Every test here operates on a real snap or flatpak, so both VMs must own one before
    any of them runs (`conftest.package_sync_subjects`).
    """
    _ = package_sync_subjects


class TestTheAptOriginModelOnRealRepositories:
    """`PKG-FR-APT-ORIGIN-DERIVED` and `PKG-FR-APT-ORIGIN-VERIFY` against a repository apt
    really fetches from.

    Everything the unit tier proves here it proves against `apt-cache policy` output written
    by hand, which is the one thing the model is about: which version apt would install, from
    which of several repositories, once priorities and version ordering are applied. A run
    that carries a repository and its key to the target and then installs from it is the only
    place that arithmetic is done by apt rather than by the test.

    Both halves share one declaration of the vendor repository on the source, which is a
    network fetch and an `apt-get update` (#216): the second half purges what the first
    installed on the target and publishes the rival there, so the two runs differ in the
    target's own arithmetic and in nothing else.
    """

    async def test_the_vendor_repository_travels_and_then_loses_to_a_rival_on_the_target(
        self,
        pc1_executor: BashLoginRemoteExecutor,
        pc2_executor: BashLoginRemoteExecutor,
        apt_subjects: AptSubjects,
        pc1_with_pcswitcher_mod: BashLoginRemoteExecutor,
        pc2_with_pcswitcher: BashLoginRemoteExecutor,
        reset_pcswitcher_state: None,
    ) -> None:
        """A29, A41, A42.

        Run 1: pc1 has a package from a vendor repository pc2 does not have; approving the
        install carries that repository and its signing key across, and pc2's own apt then
        installs the vendor's build. The whole chain is asserted on pc2's own state -- the
        `.sources` file and the keyring the approval derived, the package in `apt-mark
        showmanual`, and `apt-cache policy` naming the vendor as where it came from.

        Between the runs pc2 loses that package again and gains a rival: another repository
        offering the same name at a higher version, and a pin holding the vendor's build at
        priority 1.

        Run 2: the vendor's repository is on pc2 and still does not win, so after the run's
        `apt-get update` pc2's candidate is somebody else's software. That install is refused
        as its own failure naming both origins, and nothing of that name is installed. A
        second, ordinary install approved in the same run lands anyway, which is what makes
        the refusal one item's failure rather than the run's.
        """
        _ = (pc1_with_pcswitcher_mod, pc2_with_pcswitcher, reset_pcswitcher_state)

        other_candidate = apt_subjects.install_direction[0]
        vendor_item_id = AptPackageItem(name=VENDOR_PACKAGE, version="").item_id
        source_filename = key_filename = ""
        repo_dir = list_filename = pin_filename = ""
        try:
            source_filename, key_filename = await install_from_the_vendor_repository(pc1_executor)
            source_dest = f"{APT_SOURCES_DIR}/{source_filename}"
            key_dest = f"{APT_KEYRINGS_DIR}/{key_filename}"

            absent = await pc2_executor.run_command(
                f"test ! -e {shlex.quote(source_dest)} && test ! -e {shlex.quote(key_dest)}",
                login_shell=False,
                timeout=10.0,
            )
            assert absent.success, "the vendor repository is already on pc2, so nothing here would be derived"

            await write_apt_sync_config(pc1_executor)

            # -- run 1: the repository travels and the vendor's build lands --------------
            first = await pc1_executor.run_command(
                f"{SKIP_INSTALL_ON_TARGET} {automation_env_assignment(vendor_item_id)}"
                f" pc-switcher sync pc2 --yes --allow-first-sync",
                timeout=600.0,
                login_shell=True,
            )
            assert first.success, (
                f"pc-switcher sync exited {first.exit_code}.\nstdout: {first.stdout}\nstderr: {first.stderr}"
            )

            landed = await pc2_executor.run_command(
                f"sudo test -f {shlex.quote(source_dest)} && sudo test -f {shlex.quote(key_dest)}",
                login_shell=False,
                timeout=10.0,
            )
            assert landed.success, (
                f"the approved install did not carry {source_dest} and {key_dest} to pc2.\n"
                f"stdout: {first.stdout}\nstderr: {first.stderr}"
            )
            manual = await pc2_executor.run_command("apt-mark showmanual", login_shell=False, timeout=15.0)
            assert VENDOR_PACKAGE in nonblank_lines(manual.stdout), (
                f"{VENDOR_PACKAGE} was not installed on pc2.\nstdout: {first.stdout}\nstderr: {first.stderr}"
            )
            policy = await pc2_executor.run_command(
                f"apt-cache policy {shlex.quote(VENDOR_PACKAGE)}", login_shell=False, timeout=30.0
            )
            assert VENDOR_REPO_URI.removeprefix("https://") in policy.stdout, (
                f"pc2 has {VENDOR_PACKAGE} but apt names no {VENDOR_REPO_URI} version for it, so the copy that "
                f"landed is not the vendor's.\n{policy.stdout}"
            )

            # -- between the runs: pc2 loses the package and gains a rival for its name ---
            purged = await pc2_executor.run_command(
                f"sudo DEBIAN_FRONTEND=noninteractive apt-get purge --assume-yes {shlex.quote(VENDOR_PACKAGE)}",
                login_shell=False,
                timeout=180.0,
            )
            assert purged.success, f"Failed to purge {VENDOR_PACKAGE} from pc2 between the runs: {purged.stderr}"
            repo_dir, list_filename, pin_filename = await publish_a_rival_candidate(pc2_executor)

            # One apt transaction on each machine, run at once: they contend on nothing.
            _ = await asyncio.gather(
                ensure_installed_and_manual(pc1_executor, other_candidate),
                ensure_absent(pc2_executor, other_candidate),
            )

            # -- run 2: the target's own arithmetic refuses the vendor's build -----------
            decisions = {
                vendor_item_id: Decision.APPLY,
                AptPackageItem(name=other_candidate, version="").item_id: Decision.APPLY,
            }
            second = await pc1_executor.run_command(
                f"{SKIP_INSTALL_ON_TARGET} {automation_env_assignment_multi(decisions)} "
                "pc-switcher sync pc2 --yes --allow-first-sync --allow-out-of-order",
                timeout=600.0,
                login_shell=True,
            )
            assert not second.success, (
                "a refused install is its own failure, so the run must not report success.\n"
                f"stdout: {second.stdout}\nstderr: {second.stderr}"
            )

            installed = parse_dpkg_installed(
                (
                    await pc2_executor.run_command(
                        "dpkg-query --show --showformat='${Package}\\t${Status}\\n'", login_shell=False, timeout=20.0
                    )
                ).stdout
            )
            assert VENDOR_PACKAGE not in installed, (
                f"pc2 installed {VENDOR_PACKAGE} from {repo_dir} -- the verification let through a build that is "
                f"not the one pc1 has"
            )
            assert other_candidate in installed, (
                f"{other_candidate} was not installed, so the refusal ended the run instead of failing one item.\n"
                f"stdout: {second.stdout}\nstderr: {second.stderr}"
            )

            collapsed = collapse_run_output(second.stdout + second.stderr)
            assert f"{VENDOR_PACKAGE} was not installed:" in collapsed, (
                f"the run did not report {VENDOR_PACKAGE} as refused.\n"
                f"stdout: {second.stdout}\nstderr: {second.stderr}"
            )
            assert (
                f"has it from {VENDOR_REPO_URI.removeprefix('https://')}, but after this run's apt-get update"
                in collapsed
            ), f"the refusal did not name the origin pc1 has it from.\n{collapsed}"
            assert f"would install it from {repo_dir}" in collapsed, (
                f"the refusal did not name the origin pc2 would have taken it from.\n{collapsed}"
            )
        finally:

            async def clean_the_source() -> None:
                if source_filename:
                    await undeclare_the_vendor_repository(pc1_executor, source_filename, key_filename)

            async def clean_the_target() -> None:
                if repo_dir:
                    await remove_the_rival_candidate(pc2_executor, repo_dir, list_filename, pin_filename)
                if source_filename:
                    await undeclare_the_vendor_repository(pc2_executor, source_filename, key_filename)

            await cleanup_in_parallel(clean_the_source(), clean_the_target())


class TestARunWithNobodyToAsk:
    """Everything one non-interactive run says, refuses to do and still carries
    (`PKG-FR-NO-TERMINAL`, `PKG-FR-LOG-DECISIONS`, `PKG-FR-SNAP-DATA-BOUNDARY`,
    `PKG-FR-REGISTRY-CONSENT`, `PKG-FR-SNAP-SIDELOAD`, `PKG-FR-JOB-ORDER`).

    The premise every claim below shares is the run's own shape: no
    `PACKAGE_REVIEW_AUTOMATION_ENV`, and no TTY on stdin or stdout (the default for a command
    run through this fixture's plain SSH exec, which requests no pty). Such a run applies
    nothing anywhere and is the only run that PRINTS every review group, so the claims whose
    subject is legitimately the run's own output live here -- a `REPORT_ONLY` flatpak diff
    changes nothing anywhere, and an apt item the target's own apt cannot resolve is proven
    to have reached a review by the group that named it. The automation hook answers a review
    without ever printing it, so neither could be read from a run that used it.

    It is also the run in which `folder_sync`'s two boundaries are observable at all. Nobody
    is there to consent to the registry push, and nobody is there to approve the one snap
    install the data boundary needs declined, so what reaches pc2 of either is whatever the
    mirror carried and nothing else. `folder_sync` is configured LAST, after all four package
    jobs, because that is where it asks the target for the snap revision map and finds the
    registry it must not overwrite.

    What the unit tier cannot show about those two: it computes the snap exclusion from a
    revision map a test wrote into it and asserts the `--filter` argument built for the
    registry, but not where either comes from -- `folder_sync` asking the real target machine
    mid-run, after the package jobs have gone, and both arguments holding against real rsync
    over the directories these files actually live in.

    Both machines' real `~/snap` is set aside for the duration: the mirror deletes, so a
    hermetic tree is the only way the transfer's outcome is exactly what this test built.
    Nothing else the run reads lives under it, so the review-group claims are untouched by
    the substitution.
    """

    async def test_a_non_interactive_run_names_every_item_applies_none_and_mirrors_only_what_it_may(  # noqa: PLR0915
        self,
        pc1_executor: BashLoginRemoteExecutor,
        pc2_executor: BashLoginRemoteExecutor,
        apt_subjects: AptSubjects,
        pc1_with_pcswitcher_mod: BashLoginRemoteExecutor,
        pc2_with_pcswitcher: BashLoginRemoteExecutor,
        reset_pcswitcher_state: None,
    ) -> None:
        """H162, J9, J12, J14, J37, J44, J49, J103, A30, J98, G28, E113, E115, K88, E49 —
        and ADR-020 D-41's `ORIGIN_MISMATCH` at VM level.

        What the run must SAY and must not APPLY, over four seeded divergences:

        - an apt package removed from pc2 and another promoted to manual there, so an item
          exists in each direction: nothing is applied, no permanent decision is recorded on
          either machine, both items are NAMED as declined, and the job itself is reported
          skipped;
        - an apt package pc1 installed from a `file:` repository pc2 has never heard of
          (ADR-020 D-34 class 3): the target's apt refuses to rehearse any transaction
          containing the name, and the whole run survives it -- proven by the review group
          that names the package rather than by a run that quietly dropped it;
        - the fixture flatpak installed on pc2 from the real Flathub with pc2's `flathub`
          then repointed at the beta repository's URL: both machines print the same origin
          NAME, so a comparison by name is provably blind to it and only D-41's URL
          comparison can produce the finding, which is reported naming both vendors and
          converged by nothing;
        - one unowned `/opt` path on pc1 with no snippet of its own, so the unreproducible
          scan has a finding of its own and its refusal to name any part of the stock
          `/usr/local` skeleton is a real claim rather than a scan that found nothing.

        What the mirror that follows may and may not carry. The `~/snap` tree pc1 offers
        holds two apps, each answering one of the two shapes pc2's own `snap list` can give:

        - the first fixture snap, which pc2 is active at a revision of: that revision's data
          directory reaches pc2 and one for a revision no snapd anywhere has ever installed
          does not;
        - the second, removed from pc2 with `--purge` and left declined by this run: no
          revision directory of it reaches pc2 at all, while `~/snap/<app>/common`, which
          belongs to no revision, does -- the witness that the mirror reached the app's tree
          and the absence is the exclusion at work.

        The registry's own directory is mirrored too, with both machines holding registries
        that disagree -- one entry each, neither known to the other -- which is exactly the
        loss `PKG-FR-REGISTRY-CONSENT` exists to put a question in front of. pc2 ends the run
        holding its own registry entry for entry, and a file of pc1's own beside it arrives,
        which is what makes the registry's survival evidence about the exclusion rather than
        about a mirror that never covered the directory.

        pc1 also carries a sideloaded snap for the whole run. Both machines' complete
        `snap list` listings are compared across the run, so "the run does nothing about it"
        includes not installing it on pc2, not removing it from pc1, and not moving anything
        else while it is there. snapd's automatic refresh is paused on both machines
        throughout (the same timed `refresh.hold` a sync engages, restored exactly
        afterwards) so a background refresh cannot change a revision between the two listings
        and be read as the run's doing.

        This run is deliberately NOT a `--dry-run`. `PKG-FR-NO-TERMINAL` ends every package
        job before `apply()` when there is nobody to ask, which is the same protection for
        pc2 and the condition the printed groups above depend on; a rehearsal would add a
        second reason for the same silence and make neither observable, and would stop
        `folder_sync` transferring anything at all.
        """
        _ = (pc1_with_pcswitcher_mod, pc2_with_pcswitcher, reset_pcswitcher_state)

        uniq = uuid4().hex[:12]

        install_candidate = apt_subjects.install_direction[0]
        removal_candidate = await create_extra_on_target_apt_package(pc1_executor, pc2_executor)
        application, _version, scope, remote_name, source_url, ref = await flatpak_subject(pc1_executor)
        scope_flag = "--user" if scope == "user" else "--system"
        sudo = "sudo " if scope == "system" else ""
        witness_path = f"/opt/pcswitcher-it-scan-{uniq}"

        # The fixture's second remote supplies a real, differently-vendored URL, so nothing
        # here invents one. Both Flathub keyrings share a sha256 (measured,
        # vm-test-fixtures.sh), which is why the URL -- never a key digest -- is the whole
        # evidence.
        beta_url, _beta_options = await flatpak_remote_row(pc1_executor, FIXTURE_UNUSED_FLATPAK_REMOTE, scope)
        assert beta_url != source_url, (
            f"pc1's {remote_name} and {FIXTURE_UNUSED_FLATPAK_REMOTE} both report {source_url!r}, so no vendor "
            "divergence can be built from the fixture remotes "
            "(tests/integration/scripts/internal/vm-test-fixtures.sh)"
        )

        home, target_home = await asyncio.gather(home_dir(pc1_executor), home_dir(pc2_executor))
        assert target_home == home, (
            "the two machines' SSH users have different home directories, so `~/snap` and the registry's directory "
            "are not one path each to mirror"
        )
        snap_root = f"{home}/snap"
        registry_dir = f"{home}/{REGISTRY_DIR_RELPATH}"

        held_app, absent_app = await snap_subjects(pc1_executor, pc2_executor, count=2)
        # Three reads of two `snap list --all` outputs, taken at once.
        held_revision, absent_source_revision, absent_target_revision = await asyncio.gather(
            snap_revision(pc2_executor, held_app),
            snap_revision(pc1_executor, absent_app),
            snap_revision(pc2_executor, absent_app),
        )
        assert held_revision and absent_source_revision and absent_target_revision, (
            f"{held_app} and {absent_app} must both be installed on both machines"
        )
        stale_revision = str(int(held_revision) + 1000) if held_revision.isdigit() else f"{held_revision}0"

        held_marker = f"{snap_root}/{held_app}/{held_revision}/pcswitcher-it-{uniq}"
        stale_dir = f"{snap_root}/{held_app}/{stale_revision}"
        stale_marker = f"{stale_dir}/pcswitcher-it-{uniq}"
        absent_revision_dir = f"{snap_root}/{absent_app}/{absent_source_revision}"
        absent_revision_marker = f"{absent_revision_dir}/pcswitcher-it-{uniq}"
        common_marker = f"{snap_root}/{absent_app}/common/pcswitcher-it-{uniq}"

        sideload_name = f"pcswitcher-it-sideload-{uniq}"
        sideload_dir = f"/var/tmp/pcswitcher-it-sideload-{uniq}"

        source_path = f"/opt/pcswitcher-it-registry-source-{uniq}"
        source_item = unowned_item_id(source_path)
        target_path = f"/opt/pcswitcher-it-registry-target-{uniq}"
        target_item = unowned_item_id(target_path)
        target_body = f"# pc2's own snippet {uniq}"
        travelling = f"{registry_dir}/pcswitcher-it-{uniq}"

        pc1_prior_hold, pc2_prior_hold = await asyncio.gather(
            capture_system_refresh_hold(pc1_executor), capture_system_refresh_hold(pc2_executor)
        )

        unlocatable = repo_dir = list_filename = ""
        source_aside = target_aside = ""
        try:
            _ = await asyncio.gather(
                engage_system_refresh_hold(pc1_executor), engage_system_refresh_hold(pc2_executor)
            )

            # One apt transaction on each machine, run at once: they contend on nothing.
            _ = await asyncio.gather(
                ensure_installed_and_manual(pc1_executor, install_candidate),
                ensure_absent(pc2_executor, install_candidate),
            )

            # pc2 drops the snap this run must find absent while pc1 reads the base its
            # sideload will declare: one snapd each, and neither knows about the other.
            # `--purge` leaves snapd no snapshot behind, so removing it here costs the next
            # scenario an install (`snap_subjects`) and nothing more.
            purged, base = await asyncio.gather(
                pc2_executor.run_command(
                    f"sudo snap remove --purge {shlex.quote(absent_app)}", login_shell=False, timeout=180.0
                ),
                installed_base_snap(pc1_executor),
            )
            assert purged.success, f"could not remove {absent_app} from pc2: {purged.stderr}"
            assert await snap_revision(pc2_executor, absent_app) is None, (
                f"{absent_app} is still installed on pc2 after `snap remove --purge`, so pc2 holds a revision of it "
                "and this run cannot exercise the branch"
            )

            await create_sideloaded_snap(pc1_executor, sideload_dir, sideload_name, base)

            # pc1 builds and installs from its own repository while pc2 installs the ref this
            # test then diverges: different machines, different package managers, and neither
            # reads what the other writes. The repository's names are recorded here rather
            # than unpacked from the result, because the `finally` undeclares it from them and
            # a failure on pc2's side would otherwise leave it configured for the whole run.
            async def declare_on_the_source() -> None:
                nonlocal unlocatable, repo_dir, list_filename
                unlocatable, repo_dir, list_filename = await install_from_a_repo_the_target_lacks(pc1_executor)

            _, install = await finish_both(
                declare_on_the_source(),
                pc2_executor.run_command(
                    f"{sudo}flatpak install {scope_flag} --assumeyes --noninteractive "
                    f"{shlex.quote(remote_name)} {shlex.quote(ref)}",
                    login_shell=False,
                    timeout=600.0,
                ),
            )
            # The precondition, asserted rather than assumed: without it the run below proves
            # nothing, because a target that CAN resolve the name never had the defect.
            refused = await pc2_executor.run_command(
                f"apt-get --dry-run install --assume-yes --no-install-recommends {shlex.quote(unlocatable)}",
                login_shell=False,
                timeout=60.0,
            )
            assert not refused.success, (
                f"pc2 resolved {unlocatable}, so this run cannot exercise the class-3 path.\n"
                f"stdout: {refused.stdout}\nstderr: {refused.stderr}"
            )

            assert install.success, (
                f"failed to install {ref} on pc2 from {remote_name}, so the two machines never share the ref this "
                f"test diverges: {install.stderr}"
            )
            target_rows = [row for row in await flatpak_app_rows(pc2_executor) if row[4] == ref]
            assert target_rows, f"{ref} is not installed on pc2 after the install; there is no shared ref to diverge"
            assert target_rows[0][2] == remote_name, (
                f"pc2 reports origin {target_rows[0][2]!r} for {ref}, not {remote_name!r} -- the two machines must "
                "print the SAME origin name for the name comparison to be provably blind to this divergence"
            )
            repoint = await pc2_executor.run_command(
                f"{sudo}flatpak remote-modify {scope_flag} --url={shlex.quote(beta_url)} {shlex.quote(remote_name)}",
                login_shell=False,
                timeout=30.0,
            )
            assert repoint.success, f"failed to repoint pc2's {remote_name} at {beta_url}: {repoint.stderr}"
            target_url, _target_options = await flatpak_remote_row(pc2_executor, remote_name, scope)
            assert target_url != source_url, (
                f"pc2's {remote_name} still reports {target_url!r} after the repoint, so both machines' copies of "
                f"{ref} still come from one vendor and this run cannot exercise ORIGIN_MISMATCH"
            )

            # Captured AFTER the sideload and the purge, so what the run is held to is the
            # machines as it finds them, and set aside AFTER the capture so the sideload's own
            # `~/snap` entry travels with the rest of the real tree.
            pc1_listing_before, pc2_listing_before = await asyncio.gather(
                pc1_executor.run_command("snap list --all", login_shell=False, timeout=20.0),
                pc2_executor.run_command("snap list --all", login_shell=False, timeout=20.0),
            )
            pc1_snaps_before = parse_snap_list_names_revisions(pc1_listing_before.stdout)
            pc2_snaps_before = parse_snap_list_names_revisions(pc2_listing_before.stdout)
            assert pc1_snaps_before.get(sideload_name, "").startswith("x"), (
                f"pc1's {sideload_name} is at revision {pc1_snaps_before.get(sideload_name)!r}, not a sideloaded "
                "`x`-prefixed one, so this run cannot exercise the sideload branch"
            )
            assert sideload_name not in pc2_snaps_before, f"{sideload_name} is somehow already on pc2"

            # One machine at a time, deliberately: a failure here must still leave the machine
            # that succeeded holding a backup directory this test can name and put back.
            source_aside = await take_paths_aside(pc1_executor, [snap_root])
            target_aside = await take_paths_aside(pc2_executor, [snap_root])

            # `current` decides which revision dir the source offers at all; without it every
            # one of an app's revision dirs is excluded and the run below proves nothing.
            markers = (held_marker, stale_marker, absent_revision_marker, common_marker)
            build = "\n".join(
                ["set -eu"]
                + [f"mkdir --parents {shlex.quote(path.rsplit('/', 1)[0])}" for path in markers]
                + [f"printf %s {uniq} > {shlex.quote(path)}" for path in markers]
                + [
                    f"ln --symbolic --no-dereference --force {shlex.quote(revision)} "
                    f"{shlex.quote(f'{snap_root}/{app}/current')}"
                    for app, revision in ((held_app, held_revision), (absent_app, absent_source_revision))
                ]
            )
            built = await pc1_executor.run_command(build, login_shell=False, timeout=30.0)
            assert built.success, f"could not build the ~/snap fixture on pc1: {built.stderr}"

            # pc1 authors the registry entry the mirror must not carry and the two unowned
            # paths the scan must meet -- one with a snippet, one without -- while pc2 authors
            # the entry that has to survive. Neither machine reads the other's work.
            async def seed_the_source_scan() -> None:
                await create_unowned_marker(pc1_executor, source_path)
                await author_snippet(pc1_executor, source_item, source_path, f"touch /tmp/pcswitcher-it-{uniq}")
                await create_unowned_marker(pc1_executor, witness_path)

            _ = await asyncio.gather(
                seed_the_source_scan(),
                author_snippet(pc2_executor, target_item, target_path, target_body),
            )

            # `folder_sync` LAST: it asks the target for the snap revision map and for the
            # registry it must not overwrite, and both answers are only what they are once the
            # package jobs have gone (`PKG-FR-JOB-ORDER`).
            await write_package_sync_config(
                pc1_executor,
                extra_sections=folder_sync_section(snap_root, registry_dir),
                apt_sync=True,
                snap_sync=True,
                flatpak_sync=True,
                manual_installs_sync=True,
                folder_sync=True,
            )
            seeded = await pc1_executor.run_command(
                f"printf %s {uniq} > {shlex.quote(travelling)}", login_shell=False, timeout=15.0
            )
            assert seeded.success, f"could not seed {travelling} on pc1: {seeded.stderr}"

            # Four reads that change nothing anywhere, taken at once.
            manual_before_result, flatpak_before, pc1_decision_before, pc2_decision_before = await asyncio.gather(
                pc2_executor.run_command("apt-mark showmanual", login_shell=False, timeout=15.0),
                flatpak_app_rows(pc2_executor),
                decision_file_exists(pc1_executor, "apt"),
                decision_file_exists(pc2_executor, "apt"),
            )
            manual_before = nonblank_lines(manual_before_result.stdout)

            # No automation env prefix and no pty on this exec -- genuinely non-interactive
            # on both stdin and stdout, D-26's actual trigger condition.
            sync_result = await pc1_executor.run_command(
                f"{SKIP_INSTALL_ON_TARGET} pc-switcher sync pc2 --yes --allow-first-sync",
                timeout=900.0,
                login_shell=True,
            )
            assert sync_result.success, (
                "non-interactive sync unexpectedly failed (D-26's skip-all must not fail the job).\n"
                f"stdout: {sync_result.stdout}\nstderr: {sync_result.stderr}"
            )

            # The same four reads, again at once.
            manual_after_result, flatpak_after, pc1_decision_after, pc2_decision_after = await asyncio.gather(
                pc2_executor.run_command("apt-mark showmanual", login_shell=False, timeout=15.0),
                flatpak_app_rows(pc2_executor),
                decision_file_exists(pc1_executor, "apt"),
                decision_file_exists(pc2_executor, "apt"),
            )
            manual_after = nonblank_lines(manual_after_result.stdout)
            assert manual_after == manual_before, (
                "non-interactive run changed pc2's apt-mark showmanual -- D-26 requires nothing applied"
            )
            assert flatpak_after == flatpak_before, (
                "the run changed pc2's installed refs; an ORIGIN_MISMATCH is reported and converged by nothing."
            )
            assert pc1_decision_after == pc1_decision_before, (
                "non-interactive run created/removed a decision file on pc1"
            )
            assert pc2_decision_after == pc2_decision_before, (
                "non-interactive run created/removed a decision file on pc2"
            )

            combined_output = sync_result.stdout + sync_result.stderr
            # A trailing space so the last finding on the line has the same right-hand
            # boundary as every other one (see the skeleton check below).
            collapsed = f"{collapse_run_output(combined_output)} "

            # `PKG-FR-LOG-DECISIONS` requires the run to NAME each item nobody could be asked
            # about, so a count would no longer say which ones were declined; and
            # `PKG-FR-NO-TERMINAL` requires the job itself to be reported skipped.
            for candidate, direction in ((install_candidate, "install"), (removal_candidate, "removal")):
                assert f"{UNASKED_ITEM_MARKER}{candidate} " in collapsed, (
                    f"{direction}-direction item {candidate} was not named as declined for this run.\n"
                    f"{combined_output}"
                )
            assert "Job apt_sync skipped: non-interactive run left every apt review item undecided" in collapsed, (
                f"the run did not report apt_sync as skipped (PKG-FR-NO-TERMINAL).\n{combined_output}"
            )

            # The group panel's own title is the witness that the class-3 package reached a
            # review at all rather than being dropped to keep the run alive.
            assert "Install apt packages" in collapsed, (
                f"the run drew no apt install review group at all.\n{combined_output}"
            )
            assert f"install {unlocatable}" in collapsed, (
                f"{unlocatable} reached no review line, so the run survived by dropping it.\n{combined_output}"
            )
            assert "Unable to locate package" not in combined_output, (
                f"apt's plan-time refusal still surfaced as a run-level failure.\n{combined_output}"
            )

            # A report group is titled by its CAUSE (`sync_core._REPORT_TITLES`), so this
            # asserts the mismatch reached the ORIGIN_MISMATCH group specifically rather than
            # any report group at all. The discriminating pair: a VERSION_MISMATCH -- what
            # this diverged pair would produce if the vendor comparison missed -- names two
            # versions and no URL at all.
            assert "Installed from different remotes (flatpak applications)" in collapsed, (
                f"the mismatch reached no origin-mismatch review group.\n{combined_output}"
            )
            assert ref in combined_output, f"the report does not name the ref {ref}.\n{combined_output}"
            assert source_url in combined_output, (
                f"the report does not name the source's vendor {source_url}.\n{combined_output}"
            )
            assert target_url in combined_output, (
                f"the report does not name the target's vendor {target_url}.\n{combined_output}"
            )

            assert f"{UNASKED_ITEM_MARKER}{witness_path} " in collapsed, (
                f"the scan did not name {witness_path}, so this run says nothing about what it names.\n"
                f"{combined_output}"
            )
            for stock in STOCK_DIRECTORIES:
                # The trailing space is the boundary: `/usr/local/bin` must not satisfy the
                # check for `/usr/local`.
                assert f"{UNASKED_ITEM_MARKER}{stock} " not in collapsed, (
                    f"the scan reported {stock}, a directory the distribution itself creates, so every user "
                    f"would be asked to write an install snippet for a stock directory on every run.\n"
                    f"{combined_output}"
                )

            assert await snap_revision(pc2_executor, absent_app) is None, (
                f"{absent_app} was installed on pc2 although nobody approved it, so pc2 holds a revision of it and "
                f"the absence below would prove nothing.\n{combined_output}"
            )

            listing = await pc2_executor.run_command(
                f"find {shlex.quote(snap_root)} -mindepth 1 | sort", login_shell=False, timeout=30.0
            )
            assert listing.success, f"could not read pc2's {snap_root}: {listing.stderr}"
            arrived = set(nonblank_lines(listing.stdout))

            assert held_marker in arrived, (
                f"{held_marker} did not reach pc2, which is itself active at revision {held_revision} of "
                f"{held_app}.\n{listing.stdout}"
            )
            assert not any(path == stale_dir or path.startswith(f"{stale_dir}/") for path in arrived), (
                f"a data directory for revision {stale_revision} of {held_app} exists on pc2, whose snapd is on "
                f"{held_revision} and never installed {stale_revision}.\n{listing.stdout}"
            )
            assert common_marker in arrived, (
                f"{common_marker} did not reach pc2, so the mirror never reached {absent_app}'s tree at all and the "
                f"absence of its revision directory says nothing.\n{listing.stdout}"
            )
            assert not any(
                path == absent_revision_dir or path.startswith(f"{absent_revision_dir}/") for path in arrived
            ), (
                f"a data directory for revision {absent_source_revision} of {absent_app} exists on pc2, whose snapd "
                f"holds no revision of that app at all.\n{listing.stdout}"
            )

            landed = await pc2_executor.run_command(f"cat {shlex.quote(travelling)}", login_shell=False, timeout=15.0)
            assert landed.success and landed.stdout.strip() == uniq, (
                f"{travelling} did not reach pc2, so the mirror never covered the directory the registry lives in "
                f"and the registry surviving below says nothing.\nstdout: {landed.stdout}\nstderr: {landed.stderr}"
            )
            entries = await SnippetRegistry(pc2_executor).load()
            assert source_item not in entries, (
                f"pc1's snippet for {source_path} is in pc2's registry although nobody was asked: the registry "
                f"reached pc2 without the question that is its only route.\nregistry holds: {sorted(entries)}"
            )
            assert set(entries) == {target_item}, (
                f"pc2's registry is no longer its own: it holds {sorted(entries)} rather than the single entry "
                f"{target_item} pc2 had before the run"
            )
            assert entries[target_item].body == target_body, (
                f"pc2's own entry for {target_path} was overwritten: its body reads {entries[target_item].body!r} "
                f"rather than {target_body!r}"
            )

            pc1_listing_after, pc2_listing_after = await asyncio.gather(
                pc1_executor.run_command("snap list --all", login_shell=False, timeout=20.0),
                pc2_executor.run_command("snap list --all", login_shell=False, timeout=20.0),
            )
            pc1_snaps_after = parse_snap_list_names_revisions(pc1_listing_after.stdout)
            pc2_snaps_after = parse_snap_list_names_revisions(pc2_listing_after.stdout)
            assert pc1_snaps_after == pc1_snaps_before, (
                f"the run changed pc1's own snaps.\nbefore: {pc1_snaps_before}\nafter: {pc1_snaps_after}"
            )
            assert pc2_snaps_after == pc2_snaps_before, (
                f"the run changed pc2's snaps although nothing about them was approved.\n"
                f"before: {pc2_snaps_before}\nafter: {pc2_snaps_after}"
            )
        finally:

            async def clean_the_source() -> None:
                await remove_unowned_marker(pc1_executor, witness_path)
                await remove_unowned_marker(pc1_executor, source_path)
                if repo_dir:
                    await undeclare_local_repository(pc1_executor, repo_dir, list_filename)
                if source_aside:
                    await put_paths_back(pc1_executor, source_aside, [snap_root])
                await remove_sideloaded_snap(pc1_executor, sideload_dir, sideload_name)
                await restore_system_refresh_hold(pc1_executor, pc1_prior_hold)

            async def clean_the_target() -> None:
                # `restore_flatpak_target_baseline` re-adds with `--if-not-exists`, which
                # cannot repair a URL, so the repointed remote is deleted here first.
                await pc2_executor.run_command(
                    f"{sudo}flatpak uninstall {scope_flag} --assumeyes {shlex.quote(application)} || true; "
                    f"{sudo}flatpak remote-delete {scope_flag} --force {shlex.quote(remote_name)} || true",
                    login_shell=False,
                    timeout=120.0,
                )
                await restore_flatpak_target_baseline(pc2_executor)
                await restore_auto_marked_package(pc2_executor, removal_candidate)
                if target_aside:
                    await put_paths_back(pc2_executor, target_aside, [snap_root])
                await restore_system_refresh_hold(pc2_executor, pc2_prior_hold)

            await cleanup_in_parallel(clean_the_source(), clean_the_target())


class TestSkipAlwaysIsInertInBothRoles:
    """D-08's permanent skip, recorded once and then held against every later run, in both
    roles a machine can play.

    The ordinary review checkbox has no UI path to SKIP_ALWAYS yet for a regular item
    (`packages.review`'s own docstring: only the unreproducible items' three-way prompt and a
    hand-constructed `ReviewOutcome` reach it today) -- this drives it through the same
    `PACKAGE_REVIEW_AUTOMATION_ENV` hook every other test in this module uses, proving the
    underlying mechanism (`PackageSyncJob._record_permanent_skips`/`filter_inert`)
    independent of that UI gap.

    Two item shapes are recorded in the same first run: an ordinary apt package, which is
    source-held for the direction it was decided in, and a package installed straight from a
    `.deb`, which is unreproducible and therefore always source-held (D-08a). One shape is
    what the hook can already express and the other is what the real UI already offers, and
    the same three runs settle both (#216).

    The last of the three is this module's only run in which pc1 is the TARGET, which is the
    premise a second, ordinary removal-direction item rides on: what a run does with an item
    the user leaves undecided can only be read off the machine that would have lost the
    package.
    """

    async def test_a_recorded_skip_always_survives_a_forced_apply_in_either_direction(
        self,
        pc1_executor: BashLoginRemoteExecutor,
        pc2_executor: BashLoginRemoteExecutor,
        apt_subjects: AptSubjects,
        pc1_with_pcswitcher_mod: BashLoginRemoteExecutor,
        pc2_with_pcswitcher: BashLoginRemoteExecutor,
        reset_pcswitcher_state: None,
    ) -> None:
        """H125, H126, H166, N1, N2, G27, A54, H31 — D-08's permanent skip across three runs,
        and the removal-direction item the last of them leaves undecided (D-07's own unticked
        group).

        Run 1 records SKIP_ALWAYS for both items and applies neither. The apt entry lands in
        pc1's apt decision file and the `.deb` entry in its manual one -- and that second
        entry is also the whole witness that a hand-installed package reached the review at
        all: SKIP_ALWAYS is recorded against an item only if the review presented it
        (`_finalize_unreproducible`), and `reset_pcswitcher_state` leaves pc1 holding no
        decision file before the run.

        What only a real apt can settle about that item: a package installed straight from a
        `.deb` has its INSTALLED version as its own candidate and no repository origin at
        all, so the detection rests on what apt genuinely prints for such a package rather
        than on policy output a test author composed. Nothing marks it manually installed
        either -- `dpkg --install` is the whole setup -- and the scan reads the INSTALLED set.

        Run 2 force-maps both items to APPLY in the same direction. If D-08's inertness
        holds, neither becomes a diff at all, so the mapping has nothing to attach to:
        proven by the apt package staying absent from pc2 despite being asked for, and by the
        `.deb` entry still reading SKIP_ALWAYS rather than being presented again.

        Run 3 reverses the roles. The decision lives on pc1, now the TARGET, and D-08
        promises inertness there too -- so force-mapping the same apt item to APPLY (which,
        if a diff existed at all, would mean REMOVE, since pc1 genuinely still has the
        package) must still leave it untouched.

        A second apt package, seeded onto pc1 and off pc2 and carrying no decision of any
        kind, rides on that same run as an ordinary removal-direction item: it lands in its
        own unticked group (D-07), so deciding it SKIP_ONCE must leave pc1's copy exactly
        where it is. Read off pc1's own `apt-mark showmanual`, which is the only thing that
        distinguishes "the item was offered and declined" from "the item was applied".

        `--allow-out-of-order` bypasses the unrelated W3 consecutive-push gate a second
        same-direction sync would otherwise trip (ADR-015) -- orthogonal to what this proves.
        """
        _ = (pc1_with_pcswitcher_mod, pc2_with_pcswitcher, reset_pcswitcher_state)

        candidate = apt_subjects.install_direction[0]
        apt_item_id = AptPackageItem(name=candidate, version="").item_id
        # A second subject, never decided in runs 1 and 2 -- the hook's default leaves every
        # item it does not name SKIP_ONCE, so it stays absent from pc2 until run 3 reverses
        # the roles and turns it into a removal-direction item on pc1.
        undecided = apt_subjects.install_direction[1]
        undecided_item_id = AptPackageItem(name=undecided, version="").item_id

        hand_deb = ""
        try:
            # Each machine's apt transactions stay ordered against each other and run
            # alongside the other machine's: two machines' apt work contends on nothing. The
            # hand-installed package's name is recorded as it arrives rather than unpacked
            # from the result, because the `finally` purges it by that name and a failure on
            # pc2's side would otherwise leave it installed.
            async def seed_the_source() -> None:
                nonlocal hand_deb
                await ensure_installed_and_manual(pc1_executor, candidate)
                await ensure_installed_and_manual(pc1_executor, undecided)
                hand_deb = await install_a_hand_downloaded_deb(pc1_executor)

            async def seed_the_target() -> None:
                await ensure_absent(pc2_executor, candidate)
                await ensure_absent(pc2_executor, undecided)

            _ = await finish_both(seed_the_source(), seed_the_target())
            deb_item_id = no_candidate_item_id(hand_deb)
            # The precondition, asserted rather than assumed: apt must name no repository for
            # the installed version, or the item this run is about was never detectable.
            policy = await pc1_executor.run_command(
                f"LC_ALL=C apt-cache policy {shlex.quote(hand_deb)}", login_shell=False, timeout=30.0
            )
            assert policy.success and "1.0" in policy.stdout, (
                f"apt says nothing about the hand-installed {hand_deb}.\n"
                f"stdout: {policy.stdout}\nstderr: {policy.stderr}"
            )
            assert "http" not in policy.stdout, (
                f"apt names a repository origin for the hand-installed {hand_deb}, so it is reproducible after all "
                f"and this run cannot exercise the branch.\n{policy.stdout}"
            )

            await write_package_sync_config(pc1_executor, apt_sync=True, manual_installs_sync=True)

            # -- run 1: record -----------------------------------------------------------
            skip_always = {apt_item_id: Decision.SKIP_ALWAYS, deb_item_id: Decision.SKIP_ALWAYS}
            first = await pc1_executor.run_command(
                f"{SKIP_INSTALL_ON_TARGET} {automation_env_assignment_multi(skip_always)}"
                f" pc-switcher sync pc2 --yes --allow-first-sync",
                timeout=300.0,
                login_shell=True,
            )
            assert first.success, (
                f"skip-always run unexpectedly failed.\nstdout: {first.stdout}\nstderr: {first.stderr}"
            )

            apt_entries = await DecisionFile("apt", pc1_executor).load()
            assert apt_item_id in apt_entries, (
                f"{candidate} not recorded in pc1's apt decision file after a skip-always decision (D-08a)"
            )
            manual_entries = await DecisionFile("manual", pc1_executor).load()
            assert deb_item_id in manual_entries, (
                f"{hand_deb} was never presented as an item needing an install snippet: no decision was recorded "
                f"for {deb_item_id} on pc1 although the review was answered SKIP_ALWAYS for it.\n"
                f"recorded: {sorted(manual_entries)}"
            )
            still_absent = await pc2_executor.run_command("apt-mark showmanual", login_shell=False, timeout=15.0)
            assert candidate not in nonblank_lines(still_absent.stdout), "skip-always must not itself install the item"

            # -- run 2: same direction, forced ------------------------------------------
            force_apply = {apt_item_id: Decision.APPLY, deb_item_id: Decision.APPLY}
            second = await pc1_executor.run_command(
                f"{SKIP_INSTALL_ON_TARGET} {automation_env_assignment_multi(force_apply)} "
                "pc-switcher sync pc2 --yes --allow-first-sync --allow-out-of-order",
                timeout=300.0,
                login_shell=True,
            )
            assert second.success, (
                f"second sync unexpectedly failed.\nstdout: {second.stdout}\nstderr: {second.stderr}"
            )
            still_absent_2 = await pc2_executor.run_command("apt-mark showmanual", login_shell=False, timeout=15.0)
            assert candidate not in nonblank_lines(still_absent_2.stdout), (
                f"{candidate} was installed on pc2 despite a source-held skip-always decision -- "
                "the item produced a diff when it should have been filtered out entirely (D-08)"
            )
            # A decision file holds nothing but skip-always entries, so the entry being
            # BYTE-IDENTICAL to run 1's -- `recorded_at` included -- is what says nobody
            # answered this item again.
            assert (await DecisionFile("manual", pc1_executor).load())[deb_item_id] == manual_entries[deb_item_id], (
                f"{hand_deb}'s recorded decision was rewritten by the second run -- the item was presented again, "
                "when D-08 makes it inert"
            )

            # -- run 3: reversed roles ---------------------------------------------------
            await write_apt_sync_config(pc2_executor)
            reversed_decisions = {apt_item_id: Decision.APPLY, undecided_item_id: Decision.SKIP_ONCE}
            reversed_result = await pc2_executor.run_command(
                f"{SKIP_INSTALL_ON_TARGET} {automation_env_assignment_multi(reversed_decisions)} "
                "pc-switcher sync pc1 --yes --allow-first-sync --allow-out-of-order",
                timeout=300.0,
                login_shell=True,
            )
            assert reversed_result.success, (
                f"reversed sync unexpectedly failed.\n"
                f"stdout: {reversed_result.stdout}\nstderr: {reversed_result.stderr}"
            )
            pc1_manual_after = nonblank_lines(
                (await pc1_executor.run_command("apt-mark showmanual", login_shell=False, timeout=15.0)).stdout
            )
            assert candidate in pc1_manual_after, (
                f"{candidate} was removed from pc1 despite a target-held skip-always decision -- "
                "the item produced a diff when it should have been filtered out entirely (D-08)"
            )
            assert undecided in pc1_manual_after, (
                f"{undecided} was removed from pc1 without being approved -- a removal-direction item must take "
                "effect only when the user ticks it"
            )
        finally:
            if hand_deb:
                await pc1_executor.run_command(
                    f"sudo DEBIAN_FRONTEND=noninteractive dpkg --purge {shlex.quote(hand_deb)}",
                    login_shell=False,
                    timeout=120.0,
                )


class TestWhatARealRemovalTakesWithIt:
    """The claims whose only witness is a removal that has really run, on a machine that
    really had the thing removed.

    An unreferenced signing key, snapd's own pre-removal snapshot and the package apt's
    dependency resolution carries off with an approved one all come into being at the moment
    the transaction executes; none of them can be read off a plan, a rehearsal or a machine
    that never had the package. That premise is what the claims below share and what no
    converging install run can offer, so they take one sync between them (#216): four seeds,
    all on pc2, decided in one direction.
    """

    async def test_approved_removals_take_the_key_the_snapshot_and_only_the_approved_collateral(  # noqa: PLR0915
        self,
        pc1_executor: BashLoginRemoteExecutor,
        pc2_executor: BashLoginRemoteExecutor,
        pc1_with_pcswitcher_mod: BashLoginRemoteExecutor,
        pc2_with_pcswitcher: BashLoginRemoteExecutor,
        reset_pcswitcher_state: None,
    ) -> None:
        """C63, C104, E36, E37, D37, D72 — One run, pc1 -> pc2, over everything pc2 has and
        pc1 does not:

        - a vendor repository file that exists only on pc2 is removed, and its signing key
          goes with it although the user decided only about the repository: once the
          repository file is gone nothing on pc2 references the key any more, and that count
          is taken after the deletion actually happened, which is why a real run is its only
          witness. apt's own account of which repositories it tried is the evidence, not its
          exit code -- `apt-get update` exits 0 when an index fails to fetch. While the pair
          exists apt prints an `Err:` line naming the unresolvable synthetic host, asserted
          BEFORE the run so its total absence afterwards is a real witness in both
          directions; the post-removal exit code is asserted too, for the failure the output
          check cannot see (an `/etc/apt` left syntactically unreadable);
        - a snap pc1 no longer has is removed from pc2 by an approved item, and `snap saved`
          on pc2 then lists a snapshot for it (`PKG-FR-SNAP-REMOVE-SNAPSHOT`). The subject is
          made target-only by removing it from pc1 with `--purge`, so pc1 keeps no snapshot
          of its own and the one found on pc2 can only be this run's, and it is given system
          data first: a snapshot of a snap that never held any is not the case the article is
          about;
        - two cascading pairs on pc2, each a manually-installed package whose approved
          removal would take a second, SKIPPED one with it. They carry the two answers to
          that one question. Answering "apply" removes both, past the apply-time guard, while
          the skipped candidate's OWN removal item stays skipped -- what was approved is the
          consequence, not the item. Answering "keep" leaves the approved removal unapplied
          rather than failing it, so both of that pair's packages survive and the run still
          succeeds. The two pairs stay independent inside one run because attribution is per
          candidate: each dependent is blamed on the base whose own rehearsed transaction
          reproduces it, so one answer cancels one base.

        Each cascade claim asserts the question was put in the REVIEW and not at the
        apply-time guard, by the words each writes: the review's own decision pass logs
        `reviewed <pkg> (report_only)`, while `LateCollateral` logs `reviewed <pkg>
        (collateral)`. Both answers would otherwise leave the same machine state whichever
        round asked. "Stop the sync" is the one answer not driven here -- it is not a
        `Decision` value at all (`packages.review` raises `SyncAbortedByUser` from the screen
        itself), so the automation hook cannot express it.

        Nothing here needs a removal candidate vetted against the machine's reverse
        dependencies: every apt package this run removes is one the test built, published and
        installed on pc2 itself.
        """
        _ = (pc1_with_pcswitcher_mod, pc2_with_pcswitcher, reset_pcswitcher_state)

        snap_name = (await snap_subjects(pc1_executor, pc2_executor, count=2))[1]
        # Three snapd reads across the two machines, taken at once.
        snap_source_revision, snap_target_revision, saved_before = await asyncio.gather(
            snap_revision(pc1_executor, snap_name),
            snap_revision(pc2_executor, snap_name),
            snap_saved_rows(pc2_executor),
        )
        assert snap_source_revision and snap_target_revision, f"{snap_name} is not installed on both machines"
        snapshot_sets_before = {set_id for set_id, _snap in saved_before}

        uniq = uuid4().hex[:12]
        snap_data_file = f"/var/snap/{snap_name}/common/pcswitcher-it-{uniq}"

        source_filename = key_filename = ""
        # Appended to as each pair lands, so the `finally` can undeclare a repository even
        # when the second pair's build is what failed.
        published_pairs: list[tuple[str, str, str, str]] = []
        try:
            source_filename, key_filename = await create_synthetic_repo_and_key(pc2_executor)
            source_dest = f"{APT_SOURCES_DIR}/{source_filename}"
            key_dest = f"{APT_KEYRINGS_DIR}/{key_filename}"
            broken_update = await apt_get_update(pc2_executor)
            reached_for_repo = apt_update_lines_naming(broken_update, SYNTHETIC_REPO_HOST)
            assert any(line.startswith("Err:") for line in reached_for_repo), (
                f"pc2's `apt-get update` reported no `Err:` line naming {SYNTHETIC_REPO_HOST} while the "
                "unreachable synthetic repo was configured, so apt is not actually reaching for that repo and its "
                "absence from the post-removal run below would prove nothing.\n"
                f"lines naming the host: {reached_for_repo}\n"
                f"stdout: {broken_update.stdout}\nstderr: {broken_update.stderr}"
            )

            seeded = await pc2_executor.run_command(
                f"sudo mkdir --parents {shlex.quote(f'/var/snap/{snap_name}/common')} && "
                f"printf %s pcswitcher-it-{uniq} | sudo tee {shlex.quote(snap_data_file)} > /dev/null",
                login_shell=False,
                timeout=30.0,
            )
            assert seeded.success, f"could not give {snap_name} data on pc2 to snapshot: {seeded.stderr}"

            # pc1 gives up the snap while pc2 builds and installs both cascading pairs: one
            # machine's snapd against the other's apt, neither reading the other's work. The
            # two pairs are built one after the other because they are apt transactions on the
            # same machine. Each is recorded as it arrives rather than unpacked from the
            # result, because the `finally` undeclares its repository from it and a failure on
            # pc1's side would otherwise leave that repository configured for the whole run.
            async def publish_on_the_target() -> tuple[tuple[str, str, str, str], tuple[str, str, str, str]]:
                published_pairs.append(await publish_a_cascading_pair(pc2_executor))
                published_pairs.append(await publish_a_cascading_pair(pc2_executor))
                return published_pairs[0], published_pairs[1]

            purged, published = await finish_both(
                pc1_executor.run_command(
                    f"sudo snap remove --purge {shlex.quote(snap_name)}", login_shell=False, timeout=180.0
                ),
                publish_on_the_target(),
            )
            assert purged.success, f"Failed to remove {snap_name} from pc1: {purged.stderr}"
            (removed_base, removed_dependent, _, _), (kept_base, kept_dependent, _, _) = published

            await write_package_sync_config(pc1_executor, apt_sync=True, snap_sync=True)

            decisions = {
                f"apt:source:{source_filename}": Decision.APPLY,
                f"snap:{snap_name}": Decision.APPLY,
                AptPackageItem(name=removed_base, version="").item_id: Decision.APPLY,
                AptPackageItem(name=removed_dependent, version="").item_id: Decision.SKIP_ONCE,
                collateral_removal_item_id(removed_dependent): Decision.APPLY,
                AptPackageItem(name=kept_base, version="").item_id: Decision.APPLY,
                AptPackageItem(name=kept_dependent, version="").item_id: Decision.SKIP_ONCE,
                collateral_removal_item_id(kept_dependent): Decision.SKIP_ONCE,
            }
            removals = await pc1_executor.run_command(
                f"{SKIP_INSTALL_ON_TARGET} {automation_env_assignment_multi(decisions)}"
                f" pc-switcher sync pc2 --yes --allow-first-sync",
                timeout=600.0,
                login_shell=True,
            )
            assert removals.success, (
                "keeping a collateral package must leave the change that causes it unapplied, not fail the run.\n"
                f"exit {removals.exit_code}\nstdout: {removals.stdout}\nstderr: {removals.stderr}"
            )

            gone = await pc2_executor.run_command(
                f"test ! -e {shlex.quote(source_dest)} && test ! -e {shlex.quote(key_dest)}",
                login_shell=False,
                timeout=10.0,
            )
            assert gone.success, (
                f"{source_filename} and/or {key_filename} still present under /etc/apt on pc2 after the repository "
                f"removal was approved -- the key it left unreferenced was not collected.\n"
                f"stdout: {removals.stdout}\nstderr: {removals.stderr}"
            )
            working_update = await apt_get_update(pc2_executor)
            still_reaching = apt_update_lines_naming(working_update, SYNTHETIC_REPO_HOST)
            assert not still_reaching, (
                f"pc2's `apt-get update` still names {SYNTHETIC_REPO_HOST} after the repo file and its key were "
                "removed -- apt is still configured with the repository, so the pair did not actually leave "
                f"/etc/apt.\nlines naming the host: {still_reaching}\n"
                f"stdout: {working_update.stdout}\nstderr: {working_update.stderr}"
            )
            assert working_update.success, (
                "pc2's `apt-get update` exits non-zero after the repo file and its key were removed -- /etc/apt was "
                f"left unreadable.\nstdout: {working_update.stdout}\nstderr: {working_update.stderr}"
            )

            snap_still_there = await pc2_executor.run_command(
                f"snap list {shlex.quote(snap_name)}", login_shell=False, timeout=15.0
            )
            assert not snap_still_there.success, (
                f"{snap_name} is still installed on pc2, so no removal happened and the snapshot check below would "
                f"say nothing.\n{snap_still_there.stdout}"
            )
            saved = await snap_saved_rows(pc2_executor)
            assert any(snap == snap_name for _set_id, snap in saved), (
                f"snapd kept no snapshot for {snap_name} after the sync removed it from pc2 — the removal took the "
                f"machine's data with it.\nsnap saved: {saved}"
            )

            target_installed = parse_dpkg_installed(
                (
                    await pc2_executor.run_command(
                        "dpkg-query --show --showformat='${Package}\\t${Status}\\n'", login_shell=False, timeout=20.0
                    )
                ).stdout
            )
            assert removed_base not in target_installed, (
                f"{removed_base}'s approved removal did not run after the collateral approval"
            )
            assert removed_dependent not in target_installed, (
                f"{removed_dependent} survived a removal the user approved -- the apply-time guard refused an "
                "approved consequence"
            )
            assert kept_dependent in target_installed, (
                f"{kept_dependent} was removed from pc2 although the user kept it at the collateral question"
            )
            assert kept_base in target_installed, (
                f"{kept_base}'s approved removal still ran after {kept_dependent} was kept -- keeping a collateral "
                "package must cancel the change that causes it"
            )

            collapsed = collapse_run_output(removals.stdout + removals.stderr)
            assert f"reviewed {removed_dependent} (report_only): applied" in collapsed, (
                f"the approval for {removed_dependent} was never recorded against a collateral item in the review.\n"
                f"stdout: {removals.stdout}\nstderr: {removals.stderr}"
            )
            assert f"reviewed {kept_dependent} (report_only): skipped this run" in collapsed, (
                f"{kept_dependent} was never put to the user as a collateral question in the review.\n"
                f"stdout: {removals.stdout}\nstderr: {removals.stderr}"
            )
            for dependent in (removed_dependent, kept_dependent):
                assert f"reviewed {dependent} (collateral)" not in collapsed, (
                    f"{dependent} was asked about at the apply-time guard instead of in the review's second round"
                )
            assert (
                f"reviewed {removed_dependent} ({SYNTHETIC_PACKAGE_VERSION}) (remove): skipped this run" in collapsed
            ), (
                f"{removed_dependent}'s own removal item did not stay skipped -- the approval answered the "
                f"consequence, not the item.\nstdout: {removals.stdout}\nstderr: {removals.stderr}"
            )
        finally:
            # pc2 alone: every seed this test made lives there, and the run never had a
            # direction in which one of them could reach pc1.
            for _base, _dependent, repo_dir, list_filename in published_pairs:
                await undeclare_local_repository(pc2_executor, repo_dir, list_filename)
            for set_id, snap in await snap_saved_rows(pc2_executor):
                if snap == snap_name and set_id not in snapshot_sets_before:
                    await pc2_executor.run_command(
                        f"sudo snap forget {shlex.quote(set_id)}", login_shell=False, timeout=60.0
                    )
            leftovers = " ".join(
                shlex.quote(path)
                for path in (
                    snap_data_file,
                    f"{APT_SOURCES_DIR}/{source_filename}" if source_filename else "",
                    f"{APT_KEYRINGS_DIR}/{key_filename}" if key_filename else "",
                )
                if path
            )
            await pc2_executor.run_command(f"sudo rm --force {leftovers}", login_shell=False, timeout=15.0)


class TestAFailureCostsItsOwnItemAndNothingElse:
    """D-27, `PKG-FR-JOB-INDEPENDENCE` and `PKG-FR-SNAP-FAIL-ITEM` in the one run that can
    show all three: a job that fails on its own middle item, a snap install its target's
    snapd cannot carry out, and every item and job ordered after either still reviewing and
    converging its own diff.

    A run that FAILS is what all three need, and no converging run can be one, so this keeps
    a sync. The two defect states share it because they cannot mask each other: they are at
    different scales in different jobs, one a snippet's exit code inside
    `manual_installs_sync` and the other snapd's own refusal inside `snap_sync`, and each has
    its own witness on pc2. Neither failure can produce the other's evidence, so a run
    carrying both still says which one broke.

    The failing snippet has to come FIRST for the job-scale claim to mean anything: a job
    that fails last leaves the others' work intact whatever the orchestrator does, and an
    item that fails last says nothing about the item after it. Jobs run in the order the
    config names them (`_discover_and_validate_jobs` iterates `sync_jobs` as written), so
    `manual_installs_sync` is written first, then `apt_sync`, `snap_sync` and
    `flatpak_sync`; within the first, `_scan_unowned_installs` sorts its findings
    alphabetically by path, which is what
    places the failing snippet strictly BETWEEN the two that install something
    (`CONTINUE_TEST_MARKERS`, a < b < c).

    Both failures must genuinely reach the converge path. A package name that resolves to
    nothing is classified REPO_UNAVAILABLE/REPORT_ONLY (plan 02-05) and short-circuits before
    ever touching the target, so it would prove nothing about D-27 -- hence a snippet that
    deliberately exits non-zero. The snap failure is real snapd's, not a mock's: pc2 is put
    offline as far as the store is concerned (`snap set system store.access=offline`), which
    is precisely the split `PKG-FR-SNAP-FAIL-ITEM` needs -- an install has to reach the store
    and a removal does not.
    """

    async def test_the_item_after_a_failure_and_the_jobs_after_its_job_all_still_land(  # noqa: PLR0915
        self,
        pc1_executor: BashLoginRemoteExecutor,
        pc2_executor: BashLoginRemoteExecutor,
        apt_subjects: AptSubjects,
        pc1_with_pcswitcher_mod: BashLoginRemoteExecutor,
        pc2_with_pcswitcher: BashLoginRemoteExecutor,
        reset_pcswitcher_state: None,
    ) -> None:
        """J20, J26, J34, H12, K20, N22, E53 — All four package jobs enabled, and two
        deliberate defects among them.

        `manual_installs_sync` runs first, holding three approved snippets: two that
        genuinely `apt-get install` a real package and, between them, one that exits 42.
        `snap_sync` runs third, holding two approved items on a machine whose store is
        unreachable -- an install, which needs the store, and a removal, which does not.
        Ordering inside it is what makes "the rest still landed" a real claim rather than an
        accident: `_diff_snap_items` walks the SOURCE's snaps before the target-only ones, so
        the install is converged before the removal, and the install is the item that fails.

        The sync's own exit code is non-zero -- the orchestrator derives it from job results,
        not from whether an exception propagated (`_summarize_job_outcomes`) -- and the
        snippet's stderr lands in the run's own summary.

        The witnesses are pc2's own package managers, as everywhere else in this module. The
        snippet ordered AFTER the failing one installed its package, which is D-27's
        "continue, collect, report" promise. The apt package is back in `apt-mark showmanual`
        and the snap removal ordered after the failed install has taken its snap off `snap
        list`, each of which could only happen if that manager reviewed its own diff and then
        applied it, after the run had already failed a job -- which is also how each manager
        settling its OWN review before its OWN mutation is carried here: no inter-manager
        ordering is asserted and no run-log line is scraped for it.

        `flatpak_sync` is enabled last and left unanswered: this run's claim is about four
        jobs being enabled together, and a job whose items are all declined still plans,
        reviews and reports -- it just converges nothing, which is why nothing is asserted
        about it.

        Both snap subjects are fixture snaps, made divergent by removing one from each
        machine with `--purge` (no snapshot to clean up afterwards). Neither is put back:
        every scenario that wants a fixture snap converges to it itself (`snap_subjects`), so
        what this one leaves removed costs the next one an install only if it needs one.
        """
        _ = (pc1_with_pcswitcher_mod, pc2_with_pcswitcher, reset_pcswitcher_state)

        snippet_first, snippet_second, apt_candidate = apt_subjects.install_direction
        # Resolved before the store goes offline: converging them needs it.
        removal_subject, install_subject = await snap_subjects(pc1_executor, pc2_executor, count=2)
        # The two revisions the run acts on, read at once. The other two are irrelevant: this
        # test purges them.
        install_revision, removal_revision = await asyncio.gather(
            snap_revision(pc1_executor, install_subject), snap_revision(pc2_executor, removal_subject)
        )
        assert install_revision, f"{install_subject} is not installed on pc1, so there is no install to fail"
        assert removal_revision, f"{removal_subject} is not installed on pc2, so there is no removal to land"

        store_offline = False
        try:
            # One chain per machine, run at once: three apt transactions and one snapd
            # removal on each side, which serialise on their own machine's locks and on
            # nothing else (#216).
            async def seed_the_source() -> None:
                for subject in apt_subjects.install_direction:
                    await ensure_installed_and_manual(pc1_executor, subject)
                purged = await pc1_executor.run_command(
                    f"sudo snap remove --purge {shlex.quote(removal_subject)}", login_shell=False, timeout=180.0
                )
                assert purged.success, f"Failed to remove {removal_subject} from pc1: {purged.stderr}"

            async def seed_the_target() -> None:
                for subject in apt_subjects.install_direction:
                    await ensure_absent(pc2_executor, subject)
                purged = await pc2_executor.run_command(
                    f"sudo snap remove --purge {shlex.quote(install_subject)}", login_shell=False, timeout=180.0
                )
                assert purged.success, f"Failed to remove {install_subject} from pc2: {purged.stderr}"

            _ = await finish_both(seed_the_source(), seed_the_target())

            offline = await pc2_executor.run_command(SNAP_STORE_OFFLINE_CMD, login_shell=False, timeout=60.0)
            assert offline.success, (
                f"`{SNAP_STORE_OFFLINE_CMD}` failed, so pc2's snapd cannot be made to refuse an install and this "
                f"run has no per-item failure to observe: {offline.stderr}"
            )
            store_offline = True
            # The precondition, asserted rather than assumed: a store pc2 can still reach
            # would install the snap and leave nothing to fail.
            reachable = await pc2_executor.run_command(
                f"snap info {shlex.quote(install_subject)}", login_shell=False, timeout=60.0
            )
            assert not reachable.success, (
                f"pc2 still reaches the store for {install_subject}, so the install below would succeed.\n"
                f"stdout: {reachable.stdout}\nstderr: {reachable.stderr}"
            )

            for path in CONTINUE_TEST_MARKERS:
                await create_unowned_marker(pc1_executor, path)

            item_id_first = unowned_item_id(CONTINUE_TEST_MARKER_INSTALL_FIRST)
            item_id_fail = unowned_item_id(CONTINUE_TEST_MARKER_FAIL)
            item_id_second = unowned_item_id(CONTINUE_TEST_MARKER_INSTALL_SECOND)
            await author_snippet(
                pc1_executor,
                item_id_first,
                CONTINUE_TEST_MARKER_INSTALL_FIRST,
                f"sudo DEBIAN_FRONTEND=noninteractive apt-get install --assume-yes {shlex.quote(snippet_first)}",
            )
            await author_snippet(
                pc1_executor,
                item_id_fail,
                CONTINUE_TEST_MARKER_FAIL,
                f'echo "{DELIBERATE_FAILURE_MESSAGE}" >&2; exit 42',
            )
            await author_snippet(
                pc1_executor,
                item_id_second,
                CONTINUE_TEST_MARKER_INSTALL_SECOND,
                f"sudo DEBIAN_FRONTEND=noninteractive apt-get install --assume-yes {shlex.quote(snippet_second)}",
            )

            # Written in execution order: the failing job first, then the job whose work must
            # survive it, then the job with a failing item of its own, then the one left
            # unanswered.
            await write_package_sync_config(
                pc1_executor,
                manual_installs_sync=True,
                apt_sync=True,
                snap_sync=True,
                flatpak_sync=True,
            )

            decisions = {
                item_id_first: Decision.APPLY,
                item_id_fail: Decision.APPLY,
                item_id_second: Decision.APPLY,
                AptPackageItem(name=apt_candidate, version="").item_id: Decision.APPLY,
                f"snap:{install_subject}": Decision.APPLY,
                f"snap:{removal_subject}": Decision.APPLY,
            }
            sync_result = await pc1_executor.run_command(
                f"{SKIP_INSTALL_ON_TARGET} {automation_env_assignment_multi(decisions)}"
                f" pc-switcher sync pc2 --yes --allow-first-sync",
                timeout=600.0,
                login_shell=True,
            )
            assert not sync_result.success, (
                "a run with a failed item and a failed job must exit non-zero (D-27, PKG-FR-OUTCOME-FAILED).\n"
                f"stdout: {sync_result.stdout}\nstderr: {sync_result.stderr}"
            )
            # Read before pc2's managers are: a non-zero exit says the run failed, not that it
            # reached the two items this test fails deliberately. A run that ended earlier -- a
            # validate() abort, say -- satisfies the exit code and then fails every package
            # assertion below, which reads as a job misbehaving rather than as a run that never
            # started one.
            collapsed = collapse_run_output(sync_result.stdout + sync_result.stderr)
            assert DELIBERATE_FAILURE_MESSAGE in sync_result.stdout + sync_result.stderr, (
                "the run failed, but not on this test's deliberate snippet -- it ended before reaching it.\n"
                f"stdout: {sync_result.stdout}\nstderr: {sync_result.stderr}"
            )
            assert "1 snap item(s) failed" in collapsed, (
                f"the run did not report exactly one failed snap item.\n{sync_result.stdout}\n{sync_result.stderr}"
            )

            after_lines = nonblank_lines(
                (await pc2_executor.run_command("apt-mark showmanual", login_shell=False, timeout=15.0)).stdout
            )
            assert snippet_first in after_lines, f"{snippet_first} (before the failing item) not installed on pc2"
            assert snippet_second in after_lines, (
                f"{snippet_second} (after the failing item) not installed on pc2 -- "
                "D-27's 'continue, collect, report' promise did not hold"
            )
            assert apt_candidate in after_lines, (
                f"{apt_candidate} not reinstalled on pc2 -- apt_sync's approved work did not survive the earlier "
                "job's failure (PKG-FR-JOB-INDEPENDENCE)"
            )

            # Two read-only listings on the same machine, taken at once.
            failed_item, landed_item = await asyncio.gather(
                pc2_executor.run_command(f"snap list {shlex.quote(install_subject)}", login_shell=False, timeout=15.0),
                pc2_executor.run_command(f"snap list {shlex.quote(removal_subject)}", login_shell=False, timeout=15.0),
            )
            assert not failed_item.success, (
                f"{install_subject} was installed on pc2 although its store is unreachable, so nothing failed and "
                f"this run proves nothing.\n{failed_item.stdout}"
            )
            assert not landed_item.success, (
                f"{removal_subject} is still on pc2: the snap item ordered after the failing one was never "
                f"converged.\nstdout: {sync_result.stdout}\nstderr: {sync_result.stderr}"
            )
        finally:

            async def clean_the_source() -> None:
                for path in CONTINUE_TEST_MARKERS:
                    await remove_unowned_marker(pc1_executor, path)

            async def clean_the_target() -> None:
                for path in CONTINUE_TEST_MARKERS:
                    await remove_unowned_marker(pc2_executor, path)
                if store_offline:
                    restored = await pc2_executor.run_command(SNAP_STORE_ONLINE_CMD, login_shell=False, timeout=60.0)
                    if not restored.success:
                        print(f"[cleanup] failed to put pc2's snapd back online: {restored.stderr}")

            await cleanup_in_parallel(clean_the_source(), clean_the_target())


class TestTheESMAttachmentGateOnVMs:
    """ADR-020 D-38 at VM level: a source carrying the two `ubuntu-esm-*` sources and a
    target with no Ubuntu Pro attachment.

    Only the SKIP arm is testable here, and that is a statement about the fixtures, not a
    gap in the gate: `pro attach` needs the user's own subscription token from their Pro
    dashboard or an interactive browser short-code flow, a machine's credentials are not
    transferable, and putting a subscription token in CI would violate the project's
    secrets rule. Both VMs are therefore permanently unattached — which is exactly the
    machine this test needs, and is why nothing here is skipped or discovered: the test
    puts both machines in the state the gate needs and restores them in a `finally`.

    It keeps a sync of its own because the gate costs the WHOLE apt job: no converging run
    can carry it, since a skipped apt_sync converges nothing at all.

    What only a VM can prove: that the skip costs the WHOLE job. `/etc/apt/preferences.d`
    always-syncs with no derivation predicate, so an implementation that withheld only the
    two sources would still put the source's pin on the target — visible here as a file on
    pc2, and invisible to any mocked-executor unit test. That pin is the uuid-suffixed
    synthetic one, not `ubuntu-pro-esm-apps`: `ubuntu-pro-client` SHIPS
    `/etc/apt/preferences.d/ubuntu-pro-esm-apps` and `-esm-infra` (`dpkg -L`, measured on
    both VMs) whether or not the machine is attached, so the real ESM pins are byte-identical
    on source and target and can never be a pending write to witness anything with.
    """

    async def test_an_unattached_target_skips_apt_sync_and_leaves_etc_apt_untouched(
        self,
        pc1_executor: BashLoginRemoteExecutor,
        pc2_executor: BashLoginRemoteExecutor,
        pc1_with_pcswitcher_mod: BashLoginRemoteExecutor,
        pc2_with_pcswitcher: BashLoginRemoteExecutor,
        reset_pcswitcher_state: None,
    ) -> None:
        """H54, J10, N18."""
        _ = (pc1_with_pcswitcher_mod, pc2_with_pcswitcher, reset_pcswitcher_state)

        esm_dests = [f"{APT_SOURCES_DIR}/{name}" for name in ESM_SOURCE_BODIES]
        source_aside = ""
        target_aside = ""
        pin_dest = ""
        try:
            # Both machines are PUT in the state the gate needs rather than asked to be in
            # it already: pc2 carrying neither file — a target copy with the source's digest
            # is not a pending write, so the gate would never fire — and pc1 carrying both
            # with the bodies below, whatever either machine came with.
            target_aside = await take_paths_aside(pc2_executor, esm_dests)
            source_aside = await take_paths_aside(pc1_executor, esm_dests)
            writes = [
                f"printf %s {shlex.quote(body)} | sudo tee {shlex.quote(f'{APT_SOURCES_DIR}/{name}')} > /dev/null"
                for name, body in ESM_SOURCE_BODIES.items()
            ]
            created = await pc1_executor.run_command(" && ".join(writes), login_shell=False, timeout=20.0)
            assert created.success, f"Failed to create the ESM sources on pc1: {created.stderr}"

            pin_dest = f"{APT_PREFERENCES_DIR}/{await create_synthetic_pin(pc1_executor)}"

            # snap_sync runs after apt_sync and is the evidence that a skip is not an abort.
            await write_package_sync_config(pc1_executor, apt_sync=True, snap_sync=True)

            # No automation env and no pty: `ask_gate` finds no TTY, which is the
            # non-interactive path the user ruled must skip the whole job.
            sync_result = await pc1_executor.run_command(
                f"{SKIP_INSTALL_ON_TARGET} pc-switcher sync pc2 --yes --allow-first-sync",
                timeout=300.0,
                login_shell=True,
            )
            assert sync_result.success, (
                f"a skipped job must not fail the run.\nstdout: {sync_result.stdout}\nstderr: {sync_result.stderr}"
            )

            combined_output = sync_result.stdout + sync_result.stderr
            assert "apt_sync skipped" in combined_output, f"apt_sync was not reported as skipped.\n{combined_output}"
            for name in ESM_SOURCE_BODIES:
                assert name in combined_output, f"the skip reason does not name {name}.\n{combined_output}"
            assert "snap_sync" in combined_output, (
                f"the job after apt_sync did not run — a skip must not abort the sync.\n{combined_output}"
            )

            # The load-bearing assertion: pc2's /etc/apt is exactly as it was, the PIN
            # included. A gate that withheld only the two sources would leave the pin here.
            untouched = await pc2_executor.run_command(
                " && ".join(f"test ! -e {shlex.quote(path)}" for path in (*esm_dests, pin_dest)),
                login_shell=False,
                timeout=10.0,
            )
            assert untouched.success, (
                "a skipped apt_sync still wrote to pc2's /etc/apt — the whole job must leave it as it was"
            )
        finally:
            # uuid-suffixed, so this can never name a file either machine came with.
            cleanup = shlex.quote(pin_dest)

            async def clean_the_source() -> None:
                if pin_dest:
                    await pc1_executor.run_command(f"sudo rm --force {cleanup}", login_shell=False, timeout=15.0)
                if source_aside:
                    await put_paths_back(pc1_executor, source_aside, esm_dests)

            async def clean_the_target() -> None:
                if pin_dest:
                    await pc2_executor.run_command(f"sudo rm --force {cleanup}", login_shell=False, timeout=15.0)
                if target_aside:
                    await put_paths_back(pc2_executor, target_aside, esm_dests)

            await cleanup_in_parallel(clean_the_source(), clean_the_target())


class TestAStrayAptHoldEndsTheRun:
    """`PKG-FR-HOLD-WITHOUT-PACKAGE` against real `apt-mark` state: a hold naming a package
    its machine does not have ends the run before anything is written.

    It keeps a sync of its own for the reason the ESM gate does -- the run it needs is one
    that ABORTS, and no converging run can be that.
    """

    async def test_a_hold_naming_a_package_the_machine_does_not_have_ends_the_run(
        self,
        pc1_executor: BashLoginRemoteExecutor,
        pc2_executor: BashLoginRemoteExecutor,
        apt_subjects: AptSubjects,
        pc1_with_pcswitcher_mod: BashLoginRemoteExecutor,
        pc2_with_pcswitcher: BashLoginRemoteExecutor,
        reset_pcswitcher_state: None,
    ) -> None:
        """B16 — pc2 records a hold for a package it does not have; the run ends naming the
        package and pc2, and pc2's package state is byte-identical afterwards.

        The run is given real work first -- a package removed from pc2 that it would
        otherwise install -- so "nothing was written" is a claim about a run that had
        something to write. `MachinePackageState` is the comparison, because the article's
        "before anything is written" reaches further than the one package.
        """
        _ = (pc1_with_pcswitcher_mod, pc2_with_pcswitcher, reset_pcswitcher_state)

        ghost = await a_name_apt_knows_the_machine_does_not_have(pc2_executor)
        install_candidate = apt_subjects.install_direction[0]
        try:
            # One chain per machine, run at once: pc2's hold follows its own removal because
            # both are apt writes on that machine, and neither waits on pc1's install.
            async def seed_the_target() -> CommandResult:
                await ensure_absent(pc2_executor, install_candidate)
                return await pc2_executor.run_command(
                    f"sudo apt-mark hold {shlex.quote(ghost)}", login_shell=False, timeout=30.0
                )

            _, held = await asyncio.gather(
                ensure_installed_and_manual(pc1_executor, install_candidate), seed_the_target()
            )
            assert held.success, f"Failed to hold {ghost} on pc2: {held.stderr}"
            recorded = await pc2_executor.run_command("apt-mark showhold", login_shell=False, timeout=15.0)
            assert ghost in nonblank_lines(recorded.stdout), (
                f"pc2 did not record a hold for {ghost}, so the bookkeeping failure this test is about does not "
                f"exist on it.\n{recorded.stdout}"
            )

            await write_apt_sync_config(pc1_executor)
            before = await capture_machine_package_state(pc2_executor)

            item_id = AptPackageItem(name=install_candidate, version="").item_id
            sync_cmd = (
                f"{SKIP_INSTALL_ON_TARGET} {automation_env_assignment(item_id)}"
                f" pc-switcher sync pc2 --yes --allow-first-sync"
            )
            sync_result = await pc1_executor.run_command(sync_cmd, timeout=300.0, login_shell=True)
            assert not sync_result.success, (
                "a hold naming a package the machine does not have must end the run.\n"
                f"stdout: {sync_result.stdout}\nstderr: {sync_result.stderr}"
            )

            collapsed = collapse_run_output(sync_result.stdout + sync_result.stderr)
            assert "apt holds naming packages the machine does not have installed:" in collapsed, (
                f"the run did not end over the stray hold on {ghost}.\n"
                f"stdout: {sync_result.stdout}\nstderr: {sync_result.stderr}"
            )
            assert f"pc2: {ghost} — clear with `sudo apt-mark unhold {ghost}`" in collapsed, (
                f"the run did not name {ghost}, the machine holding it and the command that clears it.\n"
                f"stdout: {sync_result.stdout}\nstderr: {sync_result.stderr}"
            )

            after = await capture_machine_package_state(pc2_executor)
            assert after == before, (
                "the run wrote to pc2 before ending over a hold it could not act on.\n"
                f"before: {before}\nafter: {after}"
            )
        finally:
            await pc2_executor.run_command(
                f"sudo apt-mark unhold {shlex.quote(ghost)}", login_shell=False, timeout=30.0
            )


class TestTheSyncWindowHoldIsTimed:
    """`PKG-FR-SNAP-REFRESH-PAUSE`'s self-healing half: the suspension a run writes is a
    timed value on each machine's own clock, so a run that dies without cleaning up leaves
    a hold that lapses rather than one that never does.

    Only a real run can show it, and only a run that never finishes: the value is written by
    the orchestrator and put back by its own cleanup, so the only moment it exists is inside
    the sync window. No completed run can carry this claim, which is why it keeps a sync of
    its own.
    """

    async def test_a_killed_run_leaves_a_timed_hold_on_each_machines_own_clock(
        self,
        pc1_executor: BashLoginRemoteExecutor,
        pc2_executor: BashLoginRemoteExecutor,
        pc1_with_pcswitcher_mod: BashLoginRemoteExecutor,
        pc2_with_pcswitcher: BashLoginRemoteExecutor,
        reset_pcswitcher_state: None,
    ) -> None:
        """E88, E89 — A sync is killed inside its own window, and what snapd is left holding on
        BOTH machines is an instant in that machine's own near future — never `forever`.

        Killed with SIGKILL so no cleanup path can run: an orchestrator that restored the
        prior value would leave nothing to read, and a run that exited normally would say
        nothing about the case the article is about.

        `dummy_success` is enabled after `snap_sync` purely to widen the window: it sleeps
        for its configured default on each machine, which is what gives the poll below
        something to catch the run in the middle of. Both machines' `refresh.hold` is
        cleared first, so "a hold is set at all" is an unambiguous signal that the run wrote
        one, and both are put back exactly as found in the `finally`.

        The comparison is against each machine's OWN clock, never this runner's: an expiry
        computed anywhere else would still look like a future instant here, and would lapse
        at the wrong moment on the machine that has to honour it.
        """
        _ = (pc1_with_pcswitcher_mod, pc2_with_pcswitcher, reset_pcswitcher_state)

        pc1_prior_hold, pc2_prior_hold = await asyncio.gather(
            capture_system_refresh_hold(pc1_executor), capture_system_refresh_hold(pc2_executor)
        )
        run_log = f"/var/tmp/pcswitcher-it-killed-sync-{uuid4().hex[:12]}.log"

        try:
            _ = await asyncio.gather(
                restore_system_refresh_hold(pc1_executor, None), restore_system_refresh_hold(pc2_executor, None)
            )
            cleared_source, cleared_target = await asyncio.gather(
                capture_system_refresh_hold(pc1_executor), capture_system_refresh_hold(pc2_executor)
            )
            assert cleared_source is None, "pc1 still holds a refresh.hold"
            assert cleared_target is None, "pc2 still holds a refresh.hold"

            await write_package_sync_config(pc1_executor, snap_sync=True, dummy_success=True)

            started = await pc1_executor.run_command(
                f"{SKIP_INSTALL_ON_TARGET} setsid nohup"
                f" pc-switcher sync pc2 --yes --allow-first-sync > {run_log} 2>&1 < /dev/null &",
                timeout=60.0,
                login_shell=True,
            )
            assert started.success, f"could not start a sync in the background: {started.stderr}"

            engaged_source: str | None = None
            engaged_target: str | None = None
            deadline = asyncio.get_running_loop().time() + HOLD_POLL_TIMEOUT_SECONDS
            while asyncio.get_running_loop().time() < deadline:
                # The one pair of commands this module issues while a sync is running: both
                # are `snap get`, which reads and writes nothing, so neither can disturb the
                # run they are watching.
                engaged_source, engaged_target = await asyncio.gather(
                    capture_system_refresh_hold(pc1_executor), capture_system_refresh_hold(pc2_executor)
                )
                if engaged_source and engaged_target:
                    break
                await asyncio.sleep(HOLD_POLL_INTERVAL_SECONDS)
            log = await pc1_executor.run_command(f"cat {run_log}", login_shell=False, timeout=30.0)
            assert engaged_source and engaged_target, (
                "the run never paused snapd auto-refresh on both machines, so there is no window to die inside "
                f"(pc1: {engaged_source!r}, pc2: {engaged_target!r}).\n{log.stdout}"
            )

            killed = await pc1_executor.run_command(KILL_RUNNING_SYNC_CMD, login_shell=False, timeout=30.0)
            assert killed.success, (
                f"no running sync to kill — the run had already finished and restored both machines.\n{log.stdout}"
            )

            for executor, machine in ((pc1_executor, "pc1"), (pc2_executor, "pc2")):
                left = await capture_system_refresh_hold(executor)
                assert left is not None, (
                    f"{machine} was left with no refresh.hold at all by a run that died inside its own window"
                )
                assert left != "forever", (
                    f"{machine} was left with an INDEFINITE snapd refresh.hold by a run that died: nothing will ever "
                    "lift it, so that machine stops refreshing its snaps for good"
                )
                lapses = parse_rfc3339_utc(left)
                now = await machine_utc_now(executor)
                assert lapses > now, (
                    f"{machine}'s refresh.hold {left!r} is not in its own future (its clock reads {now}), so the "
                    "suspension either never took effect or was computed against another machine's clock"
                )
                assert lapses - now <= SNAP_HOLD_EXPECTED_DURATION, (
                    f"{machine}'s refresh.hold {left!r} lapses {lapses - now} from now, further ahead than the "
                    f"{SNAP_HOLD_EXPECTED_DURATION} a sync window asks for"
                )
                assert lapses - now >= SNAP_HOLD_EXPECTED_DURATION - SNAP_HOLD_DURATION_SLACK, (
                    f"{machine}'s refresh.hold {left!r} lapses {lapses - now} from now, far sooner than the "
                    f"{SNAP_HOLD_EXPECTED_DURATION} a sync window asks for"
                )
        finally:
            # The kill comes first and alone: a sync still running would write both machines'
            # `refresh.hold` again after the restores below.
            await pc1_executor.run_command(KILL_RUNNING_SYNC_CMD, login_shell=False, timeout=30.0)

            async def clean_the_source() -> None:
                await restore_system_refresh_hold(pc1_executor, pc1_prior_hold)
                await pc1_executor.run_command(f"rm --force {run_log}", login_shell=False, timeout=15.0)

            await cleanup_in_parallel(clean_the_source(), restore_system_refresh_hold(pc2_executor, pc2_prior_hold))


class TestSnapHoldCaptureTiming:
    """The VM check #208 D9 promised and never got (L10), in the half that needs no sync.

    `SnapSyncJob` reads per-snap holds out of `snap list`'s Notes column DURING the sync,
    i.e. inside the window in which the orchestrator has a system-wide `refresh.hold`
    engaged on both hosts. D9 assumes those are separate snapstate -- that a system-wide
    hold neither sets nor clears an individual snap's `held` note -- and says so in a
    comment in `snap_sync._parse_snap_list`. Nothing had ever checked it against a real
    snapd.

    The end-to-end half of the same assumption, where a hold set on the source reaches the
    target through a real sync window, is one of the divergences the converging sync in
    `tests/integration/test_end_to_end_sync.py` seeds.
    """

    async def test_system_refresh_hold_does_not_mask_a_per_snap_held_note(
        self,
        pc2_executor: BashLoginRemoteExecutor,
    ) -> None:
        """E71 — With a system-wide `refresh.hold` engaged, a per-snap hold still reads `held`
        in `snap list` Notes, and a snap WITHOUT a per-snap hold still reads no `held`.

        Both directions matter. If the system hold masked the note, capture inside the
        sync window would silently drop every hold the user set (holds would never
        replicate). If it ADDED the note, capture would invent a hold for every snap on
        the machine. D9's fail-safe (a system hold flips both hosts symmetrically, so a
        spurious flag cancels out in the membership diff) covers the second case only as
        long as both hosts are held -- which is why this asserts the note itself rather
        than relying on the diff to absorb it.

        Runs no sync, so it needs neither a pc-switcher install nor the state reset: the
        subject is snapd's own semantics, read straight off `snap list`.
        """
        held_name, unheld_name = await holdable_snaps(pc2_executor, count=2)

        prior_hold = await capture_system_refresh_hold(pc2_executor)
        try:
            hold_result = await pc2_executor.run_command(
                f"sudo snap refresh --hold=forever {shlex.quote(held_name)}", login_shell=False, timeout=60.0
            )
            assert hold_result.success, f"Failed to set a per-snap hold on {held_name}: {hold_result.stderr}"

            # Baseline, before any system-wide hold exists: the per-snap hold is visible
            # at all. Without this the assertion below could pass vacuously on a snapd
            # that never writes `held` into Notes.
            assert "held" in await snap_notes(pc2_executor, held_name), (
                f"snapd did not report `held` in `snap list` Notes for {held_name} after "
                "`snap refresh --hold=forever` -- the per-snap hold mechanism this assumption is about is not visible"
            )

            await engage_system_refresh_hold(pc2_executor)
            engaged = await capture_system_refresh_hold(pc2_executor)
            assert engaged is not None, (
                "system-wide refresh.hold did not take effect; the check below would be vacuous"
            )

            notes_under_system_hold = await snap_notes(pc2_executor, held_name)
            assert "held" in notes_under_system_hold, (
                f"#208 D9 IS FALSE: with a system-wide refresh.hold engaged ({engaged}), {held_name}'s per-snap hold "
                f"no longer reads `held` in `snap list` Notes (notes: {sorted(notes_under_system_hold)}). "
                "snap_sync captures inside exactly this window, so every per-snap hold would be silently dropped -- "
                "the capture must move BEFORE the sync-window hold is applied."
            )

            unheld_notes = await snap_notes(pc2_executor, unheld_name)
            assert "held" not in unheld_notes, (
                f"#208 D9 IS FALSE in the other direction: a system-wide refresh.hold ({engaged}) put `held` "
                f"into {unheld_name}'s Notes even though no per-snap hold was set on it "
                f"(notes: {sorted(unheld_notes)}) -- capture inside the sync window would invent holds."
            )
        finally:
            await pc2_executor.run_command(
                f"sudo snap refresh --unhold {shlex.quote(held_name)}", login_shell=False, timeout=60.0
            )
            await restore_system_refresh_hold(pc2_executor, prior_hold)


class TestTheStockSkeletonTheScanRefusesToName:
    """The tripwire under the hardcoded `/usr/local` skeleton `manual_installs_sync` refuses
    to present. It needs no sync at all -- the subject is one file the distribution ships.
    """

    async def test_the_stock_skeleton_is_still_what_base_files_creates(
        self, pc1_executor: BashLoginRemoteExecutor
    ) -> None:
        """G114 — the machine's own `base-files.postinst` must still create exactly the nine
        entries directly under `/usr/local` that the scan refuses to present.

        The list is hardcoded in the job for predictability — what the scan presents must not
        change with whatever a postinst says on the day — so this is the assertion that
        catches a distribution changing it: a failure here means the skeleton moved and the
        constant has to follow, not that a run is broken.

        `/usr/local` itself and `share/man` are excluded on purpose: the first is a scan root
        rather than an entry of anything, and the second is not directly under `/usr/local`,
        which is the only level the scan can meet these at.
        """
        postinst = await pc1_executor.run_command(
            "cat /var/lib/dpkg/info/base-files.postinst", login_shell=False, timeout=15.0
        )
        assert postinst.success, f"could not read base-files.postinst: {postinst.stderr}"

        created = set(re.findall(r"^\s*install_local_dir\s+(/usr/local/[^\s/]+)\s*$", postinst.stdout, re.MULTILINE))
        symlinked = set(re.findall(r'ln -s\S*\s+\S+\s+"?\$DPKG_ROOT(/usr/local/[^\s/"]+)"?', postinst.stdout))

        assert created | symlinked == {stock for stock in STOCK_DIRECTORIES if stock.count("/") == 3}, (
            "base-files no longer creates the `/usr/local` skeleton the scan is built on; "
            f"it declares {sorted(created | symlinked)}.\n{postinst.stdout}"
        )
