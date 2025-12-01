"""Tests for version module."""

from __future__ import annotations

from importlib.metadata import PackageNotFoundError
from unittest.mock import patch

import pytest

from pcswitcher.version import get_this_version


class TestGetCurrentVersion:
    """Tests for get_this_version()."""

    def test_get_this_version_success(self) -> None:
        """Should return version from package metadata."""
        with patch("pcswitcher.version.version") as mock_version:
            mock_version.return_value = "1.2.3"
            result = get_this_version()
            assert result == "1.2.3"
            mock_version.assert_called_once_with("pcswitcher")

    def test_get_this_version_package_not_found(self) -> None:
        """Should raise PackageNotFoundError if package not found."""
        with patch("pcswitcher.version.version") as mock_version:
            mock_version.side_effect = PackageNotFoundError("pcswitcher")

            with pytest.raises(PackageNotFoundError) as exc_info:
                get_this_version()

            assert "Cannot determine pc-switcher version" in str(exc_info.value)
            assert "Package metadata not found" in str(exc_info.value)
