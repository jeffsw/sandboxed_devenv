"""Shared fixtures. Tests import the package through importlib mode (pyproject.toml)."""

import pathlib

import pytest

CHECKOUT = pathlib.Path(__file__).resolve().parent.parent


@pytest.fixture
def checkout() -> pathlib.Path:
    """The repository root, for tests that read pyproject.toml or PLAN.md."""
    retval = CHECKOUT
    return retval
