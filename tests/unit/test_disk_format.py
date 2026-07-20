"""Tests for human-readable byte formatting (disk.format_bytes)."""

from __future__ import annotations

import pytest

from pcswitcher.disk import format_bytes


@pytest.mark.parametrize(
    ("num_bytes", "expected"),
    [
        (0, "0 B"),
        (512, "512 B"),
        (1023, "1023 B"),
        (1024, "1.0 KiB"),
        (1536, "1.5 KiB"),
        (2**20, "1.0 MiB"),
        (557587395, "531.8 MiB"),  # the size from issue #189
        (2**30, "1.0 GiB"),
        (5 * 2**30, "5.0 GiB"),
        (2**40, "1.0 TiB"),
        (3 * 2**50, "3072.0 TiB"),  # petabyte range stays in TiB
    ],
)
def test_format_bytes(num_bytes: int, expected: str) -> None:
    assert format_bytes(num_bytes) == expected
