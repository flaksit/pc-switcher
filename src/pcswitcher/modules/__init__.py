"""Sync modules for pc-switcher."""

from __future__ import annotations

from pcswitcher.modules.dummy_critical import DummyCriticalModule
from pcswitcher.modules.dummy_fail import DummyFailModule
from pcswitcher.modules.dummy_success import DummySuccessModule

__all__ = ["DummySuccessModule", "DummyCriticalModule", "DummyFailModule"]
