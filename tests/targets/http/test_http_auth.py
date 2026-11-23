from collections.abc import Mapping
from typing import Any

import pytest

from pecker_drum.targets.http.auth import MissingSecretError, TokenManager, read_secrets
from pecker_drum.targets.http.client import Redactor, TargetError
from pecker_drum.targets.http.config import HttpTargetConfig

PASSWORDS = {
    "DRUM_EXAMPLE_PASSWORD_CAROL": "pw-carol-1234",
    "DRUM_EXAMPLE_PASSWORD_DANA": "pw-dana-1234",
    "DRUM_EXAMPLE_PASSWORD_ERIN": "pw-erin-1234",
}


class FakeIdp:
    def __init__(self, lifetime: float = 100.0) -> None:
        self.forms: list[dict[str, str]] = []
        self.lifetime = lifetime
        self.refresh_fails = False
        self.counter = 0

    async def post(self, step: str, form: Mapping[str, str]) -> Any:
        self.forms.append(dict(form))
        if form["grant_type"] == "refresh_token" and self.refresh_fails:
            raise TargetError(f"{step}: HTTP 400", 400)
        self.counter += 1
        return {
            "access_token": f"access-{self.counter}-abcdef",
            "refresh_token": f"refresh-{self.counter}-abcdef",
            "expires_in": self.lifetime,
        }


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def manager(config: HttpTargetConfig, idp: FakeIdp, clock: Clock) -> tuple[TokenManager, Redactor]:
    redactor = Redactor()
    secrets = read_secrets(config.auth, PASSWORDS)
    return TokenManager(config.auth, secrets, idp.post, redactor, clock), redactor


async def test_token_is_refreshed_before_it_expires(example_config: HttpTargetConfig) -> None:
    idp, clock = FakeIdp(lifetime=100.0), Clock()
    tokens, redactor = manager(example_config, idp, clock)
    first = await tokens.token_for("carol")
    assert idp.forms[0]["grant_type"] == "password"
    assert idp.forms[0]["username"] == "carol"
    assert idp.forms[0]["password"] == "pw-carol-1234"
    assert idp.forms[0]["client_id"] == "drum-redteam"
    clock.now += 60  # margin is min(30, 100 / 2): still 40 s left
    assert await tokens.token_for("carol") == first
    assert len(idp.forms) == 1
    clock.now += 15  # 25 s left: inside the margin
    second = await tokens.token_for("carol")
    assert second != first
    assert idp.forms[1] == {
        "client_id": "drum-redteam",
        "grant_type": "refresh_token",
        "refresh_token": "refresh-1-abcdef",
    }
    for secret in ("pw-carol-1234", first, second, "refresh-1-abcdef"):
        assert redactor(f"x {secret} y") == "x [redacted] y"


async def test_a_failed_refresh_logs_in_again(example_config: HttpTargetConfig) -> None:
    idp, clock = FakeIdp(lifetime=10.0), Clock()
    tokens, _ = manager(example_config, idp, clock)
    await tokens.token_for("dana")
    idp.refresh_fails = True
    clock.now += 9
    await tokens.token_for("dana")
    assert [f["grant_type"] for f in idp.forms] == ["password", "refresh_token", "password"]


async def test_unknown_users_and_bad_replies(example_config: HttpTargetConfig) -> None:
    tokens, _ = manager(example_config, FakeIdp(), Clock())
    with pytest.raises(TargetError, match="no login for user 'mallory'"):
        await tokens.token_for("mallory")

    async def broken(step: str, form: Mapping[str, str]) -> Any:
        return {"token": "x"}

    bad = TokenManager(
        example_config.auth,
        read_secrets(example_config.auth, PASSWORDS),
        broken,
        Redactor(),
        Clock(),
    )
    with pytest.raises(TargetError, match="no access_token"):
        await bad.token_for("carol")


def test_missing_passwords_are_named_never_shown(example_config: HttpTargetConfig) -> None:
    partial = {"DRUM_EXAMPLE_PASSWORD_CAROL": "pw-carol-1234"}
    with pytest.raises(MissingSecretError) as raised:
        read_secrets(example_config.auth, partial)
    message = str(raised.value)
    assert "DRUM_EXAMPLE_PASSWORD_DANA" in message
    assert "DRUM_EXAMPLE_PASSWORD_ERIN" in message
    assert "pw-carol" not in message
