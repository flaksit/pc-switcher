"""Shared package-sync helpers used by the apt/snap/flatpak/manual-installs jobs.

Holds the item model (`items`), the batched review pipeline (`review`), the
machine-local decision/snippet store (`state`), the extracted job core
(`sync_core`) and the `apt-cache policy` parsers two jobs share (`apt_policy`).
These modules are imported by the job modules in ``jobs/``; they
are not jobs themselves and are never resolved by job discovery, which maps a
``sync_jobs`` key to ``jobs/<name>.py``.
"""
