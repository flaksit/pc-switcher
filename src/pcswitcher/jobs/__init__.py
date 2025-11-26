"""Sync jobs for pc-switcher."""

from __future__ import annotations

from pcswitcher.jobs.dummy_fail import DummyFailJob
from pcswitcher.jobs.dummy_success import DummySuccessJob

__all__ = ["DummySuccessJob", "DummyFailJob"]
