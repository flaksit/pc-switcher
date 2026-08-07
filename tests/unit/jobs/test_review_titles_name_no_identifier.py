"""No review title may print a job or manager identifier (#276).

The defect this pins is a class, not three strings: the group title used to be assembled
out of `manager_id`, so `manual_deb_sync` announced itself as "Remove manual_deb packages"
and `manual_installs_sync` as "Remove manual packages" over a list of paths. Any future
title built the same way fails here, whichever job grows it.

The words a job is allowed to use are the ones it declares — `item_noun` and
`item_noun_plural` — so the test is written against those rather than against a list of
expected sentences: `apt` and `snap` legitimately appear in "apt packages" and "snaps",
and only a string that is NOT part of the job's own nouns is a leak.
"""

from __future__ import annotations

import pytest

from pcswitcher.jobs.apt_sync.job import AptSyncJob
from pcswitcher.jobs.flatpak_sync import FlatpakSyncJob
from pcswitcher.jobs.manual_deb_sync import ManualDebSyncJob
from pcswitcher.jobs.manual_flatpak_sync import ManualFlatpakSyncJob
from pcswitcher.jobs.manual_installs_sync import ManualInstallsSyncJob
from pcswitcher.jobs.manual_snap_sync import ManualSnapSyncJob
from pcswitcher.jobs.packages.items import DiffAction, DiffClass, ItemClass, ItemDiff
from pcswitcher.jobs.packages.review import (
    UNREPRODUCIBLE_REVIEW_ACTION,
    UNREPRODUCIBLE_UPDATE_REVIEW_ACTION,
    ReviewGroup,
)
from pcswitcher.jobs.packages.sync_core import BLOCK_ITEM_CLASSES, PackageSyncJob
from pcswitcher.jobs.snap_sync import SnapSyncJob
from tests.unit.jobs.test_package_sync_core import make_context
from tests.unit.test_job_display_names import _shipped_job_classes  # pyright: ignore[reportPrivateUsage]

# Every shipped package job, with the item classes its own diffs carry. Listed rather than
# swept, because each job's `_build_review_groups` carves its own classes out by item_id
# shape and a class it never produces would only exercise the carve, not the titles.
_JOB_ITEM_CLASSES: dict[type[PackageSyncJob], tuple[ItemClass, ...]] = {
    AptSyncJob: (ItemClass.APT_PACKAGE, ItemClass.APT_CONFIG),
    SnapSyncJob: (ItemClass.SNAP, ItemClass.SNAP_CHANNEL),
    FlatpakSyncJob: (ItemClass.FLATPAK_REF,),
    ManualDebSyncJob: (ItemClass.UNREPRODUCIBLE,),
    ManualSnapSyncJob: (ItemClass.UNREPRODUCIBLE,),
    ManualFlatpakSyncJob: (ItemClass.UNREPRODUCIBLE,),
    ManualInstallsSyncJob: (ItemClass.UNREPRODUCIBLE,),
}


def _all_titles(job: PackageSyncJob, item_classes: tuple[ItemClass, ...]) -> list[ReviewGroup]:
    """Every group this job can title: one diff per (item class, action, cause) it produces."""
    groups: list[ReviewGroup] = []
    for item_class in item_classes:
        if item_class in BLOCK_ITEM_CLASSES:
            continue
        for action in DiffAction:
            causes = (
                (DiffClass.VERSION_MISMATCH, DiffClass.ORIGIN_MISMATCH, DiffClass.REPO_UNAVAILABLE)
                if action is DiffAction.REPORT_ONLY
                else (DiffClass.MISSING_ON_TARGET,)
            )
            for cause in causes:
                diff = ItemDiff(
                    item_class=item_class,
                    diff_class=cause,
                    action=action,
                    item_id="x1",
                    label="x1",
                    detail=None,
                )
                groups.extend(job._build_review_groups([diff]))  # pyright: ignore[reportPrivateUsage]
    return groups


def _package_jobs() -> list[type[PackageSyncJob]]:
    shipped = [cls for cls in _shipped_job_classes() if issubclass(cls, PackageSyncJob)]
    assert {cls.name for cls in shipped} == {cls.name for cls in _JOB_ITEM_CLASSES}, (
        "a package job ships that this test does not name its item classes for"
    )
    return list(_JOB_ITEM_CLASSES)


# Every identifier a title could leak: the config/log spelling of each job, and each package
# job's manager id.
_IDENTIFIERS: frozenset[str] = frozenset(
    {cls.name for cls in _shipped_job_classes()}
    | {cls.manager_id for cls in _shipped_job_classes() if issubclass(cls, PackageSyncJob)}
)


@pytest.mark.parametrize("job_cls", _package_jobs(), ids=lambda cls: cls.name)
class TestNoTitleNamesAnIdentifier:
    def test_no_group_title_carries_an_underscore(self, job_cls: type[PackageSyncJob]) -> None:
        """#276 — every identifier in this codebase that is not also an ordinary English word
        carries an underscore: `manual_deb`, `manual_flatpak`, and every job `name`. No noun
        the review is allowed to use does, and a group title never contains an item label, so
        an underscore anywhere in one is an identifier that escaped.
        """
        job = job_cls(make_context())

        for group in _all_titles(job, _JOB_ITEM_CLASSES[job_cls]):
            assert "_" not in group.title, group.title

    def test_no_group_title_names_a_job_or_manager_it_is_not_the_word_for(self, job_cls: type[PackageSyncJob]) -> None:
        """#276 — the underscore rule cannot catch `manual` or `apt`. This one can: an
        identifier may appear in a title only where it is genuinely part of the job's own
        declared noun, which is what makes "Install apt packages on nomad?" fine and
        "Remove manual packages" not.
        """
        job = job_cls(make_context())
        allowed = {
            identifier
            for identifier in _IDENTIFIERS
            if identifier in job_cls.item_noun_plural or identifier in job_cls.item_noun
        }

        for group in _all_titles(job, _JOB_ITEM_CLASSES[job_cls]):
            leaked = [identifier for identifier in _IDENTIFIERS - allowed if identifier in group.title]
            assert not leaked, f"{group.title!r} names {leaked}"

    def test_every_per_item_group_carries_the_noun_its_titles_need(self, job_cls: type[PackageSyncJob]) -> None:
        """#276 — the per-item screens are titled in `review.py`, out of `ReviewGroup.item_noun`.
        A group that reaches one without a noun renders `Install  brscan3 on nomad?` — the
        word that names what brscan3 is simply missing, which is the state the identifier
        fallback used to paper over.
        """
        job = job_cls(make_context())
        per_item = {UNREPRODUCIBLE_REVIEW_ACTION, UNREPRODUCIBLE_UPDATE_REVIEW_ACTION}

        for group in _all_titles(job, _JOB_ITEM_CLASSES[job_cls]):
            if group.action not in per_item:
                continue
            assert group.item_noun, group.title
            assert "_" not in group.item_noun, group.item_noun

    def test_the_job_declares_its_own_nouns_rather_than_inheriting_a_default(
        self, job_cls: type[PackageSyncJob]
    ) -> None:
        """#276 — the nouns live beside `display_name` on the job class so one place holds every
        word a user reads about it. Declared in the class body, never defaulted: a default is
        what let a job be announced by its identifier without anyone noticing.
        """
        assert "item_noun" in job_cls.__dict__
        assert "item_noun_plural" in job_cls.__dict__


def test_a_package_job_without_nouns_is_refused_at_import() -> None:
    """#276 — the declaration is enforced where the class is created, so a new job cannot
    reach a user unnamed: there is no run-time fallback left to catch it later.
    """
    with pytest.raises(TypeError, match="item_noun"):
        type("_Nameless", (PackageSyncJob,), {"name": "nameless_sync", "manager_id": "nameless"})
