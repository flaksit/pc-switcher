"""Pytest configuration and shared fixtures."""

import pytest


@pytest.fixture
def sample_fixture() -> str:
    """Example fixture for testing."""
    return "test_value"
