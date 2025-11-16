"""PC-switcher - Synchronization system for seamless switching between Linux desktop machines."""

__all__ = ["__version__"]

try:
    from ._version import __version__  # type: ignore[import-not-found]
except ImportError:
    __version__ = "0.0.0+unknown"
