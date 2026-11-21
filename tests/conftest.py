"""Shared test setup: no test may reach the network."""

from __future__ import annotations

import os
from collections.abc import Generator

import pytest

from pecker_drum.netguard import allow_network, block_network

os.environ.setdefault("HF_HUB_OFFLINE", "1")


@pytest.fixture(autouse=True)
def _no_network() -> Generator[None]:
    block_network()
    try:
        yield
    finally:
        allow_network()
