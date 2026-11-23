"""Shared setup for HTTP target tests: the example config and the stand-in on 127.0.0.1."""

from __future__ import annotations

from collections.abc import Callable, Generator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from pecker_drum.items.loader import load_item_set
from pecker_drum.items.schema import Item
from pecker_drum.netguard import block_network
from pecker_drum.targets.http import HttpTarget, HttpTargetConfig, load_config
from pecker_drum.targets.stub import StubMode, StubOptions, StubServer

REPO = Path(__file__).resolve().parents[3]
EXAMPLE = REPO / "docs" / "examples" / "http-target.toml"


@dataclass
class Rig:
    server: StubServer
    target: HttpTarget
    env: dict[str, str]
    config: HttpTargetConfig


@pytest.fixture
def loopback() -> None:
    """Tests that start the stand-in may reach 127.0.0.1 and nothing else."""
    block_network(allow_loopback=True)


@pytest.fixture(scope="session")
def example_config() -> HttpTargetConfig:
    return load_config(EXAMPLE)


@pytest.fixture(scope="session")
def public_items() -> tuple[Item, ...]:
    return load_item_set(REPO / "items" / "public_v1.jsonl").items


def passwords_for(config: HttpTargetConfig) -> dict[str, str]:
    return {login.password_env: f"pw-{sub}-7f3a9c2e51" for sub, login in config.auth.users.items()}


@pytest.fixture
def make_rig(
    loopback: None, example_config: HttpTargetConfig, public_items: tuple[Item, ...]
) -> Generator[Callable[..., Rig]]:
    servers: list[StubServer] = []

    def make(
        mode: StubMode = StubMode.OBEDIENT,
        options: StubOptions | None = None,
        config: HttpTargetConfig | None = None,
        items: tuple[Item, ...] | None = None,
        **kwargs: Any,
    ) -> Rig:
        written = config or example_config
        env = passwords_for(written)
        users = {login.username: env[login.password_env] for login in written.auth.users.values()}
        server = StubServer(items or public_items, mode, users, options).start()
        servers.append(server)
        runtime = written.model_copy(update={"base_url": server.url})
        kwargs.setdefault("fixtures_root", REPO / "fixtures")
        target = HttpTarget(
            runtime, name="stub", model=str(mode), identity=written, environ=env, **kwargs
        )
        return Rig(server, target, env, written)

    yield make
    for server in servers:
        server.stop()
