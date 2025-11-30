"""PC-switcher: Synchronization system for seamless switching between Linux desktop machines."""

from __future__ import annotations

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("pcswitcher")
except PackageNotFoundError:
    __version__ = "0.0.0.dev0"

__all__ = ["__version__"]
