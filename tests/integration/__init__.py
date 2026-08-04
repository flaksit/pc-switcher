"""Integration tests, and what every one of them may say about a `pc-switcher sync`."""

from pcswitcher.jobs.install_on_target import INSTALL_ON_TARGET_SKIP_ENV

#: Prefix for a `pc-switcher sync` whose subject is not self-installation: it drops the
#: install-on-target step, worth ~1.75s of `pc-switcher --version` over a login shell per
#: run. Safe wherever the target does not need pc-switcher PUT there by the sync — no sync
#: command invokes the binary on the target, so nothing else in a run depends on the step.
#: Three modules must never carry it: `jobs/test_install_on_target_job.py`, which proves
#: installing and upgrading; `test_end_to_end_sync.py`, whose subject is the whole pipeline
#: and so includes the step that puts pc-switcher on the target; and `test_dry_run.py`, where
#: the rehearsal has to PREVIEW that step.
SKIP_INSTALL_ON_TARGET = f"{INSTALL_ON_TARGET_SKIP_ENV}=1"
