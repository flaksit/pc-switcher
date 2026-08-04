"""Integration tests, and what every one of them may say about a `pc-switcher sync`."""

from pcswitcher.jobs.install_on_target import INSTALL_ON_TARGET_SKIP_ENV

#: Prefix for a `pc-switcher sync` whose subject is not self-installation: it drops the
#: install-on-target step, worth ~1.75s of `pc-switcher --version` over a login shell per
#: run. Safe wherever the target does not need pc-switcher PUT there by the sync — no sync
#: command invokes the binary on the target, so nothing else in a run depends on the step.
#: The tests that prove installing and upgrading (`TestInstallOnTargetIntegration`,
#: `jobs/test_install_on_target_job.py::TestSelfInstallation`) must never carry it.
SKIP_INSTALL_ON_TARGET = f"{INSTALL_ON_TARGET_SKIP_ENV}=1"

#: Source config for a sync whose subject is the run itself, not what a job transfers: the
#: only sync job is `dummy_success`, whose per-host duration the caller formats in. Used by
#: the `sync_ready_source` fixture and test_end_to_end_sync.py.
SYNC_TEST_CONFIG_TEMPLATE = """# Test configuration for end-to-end sync tests
# Short durations to keep tests fast

logging:
  file: DEBUG
  tui: DEBUG
  external: DEBUG

sync_jobs:
  dummy_success: true
  dummy_fail: false

disk_space_monitor:
  preflight_minimum: "5%"
  runtime_minimum: "3%"
  warning_threshold: "10%"
  check_interval: 5

btrfs_snapshots:
  subvolumes:
    - "@"
    - "@home"
  keep_recent: 2

dummy_success:
  source_duration: {source_duration}
  target_duration: {target_duration}
"""
