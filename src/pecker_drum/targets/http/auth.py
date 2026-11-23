"""OIDC password login for test users, with refresh before expiry (M2, ADR 0009).

Each ``item.user.sub`` maps to a user name and the name of a ``DRUM_...`` variable holding the
password. A token is refreshed ``refresh_margin`` seconds before it expires (at most half its
lifetime): with the refresh token while it is valid, else with the password. Every token and
password goes to the redactor, and a login error never shows the response body.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass

from pydantic import JsonValue

from pecker_drum.targets.http.client import Clock, Redactor, TargetError
from pecker_drum.targets.http.config import OidcPasswordAuth

DEFAULT_LIFETIME_S = 300.0

Post = Callable[[str, Mapping[str, str]], Awaitable[JsonValue]]
"""Sends a form to the token endpoint and returns the JSON reply (raises ``TargetError``)."""


class MissingSecretError(RuntimeError):
    """A variable the config names is not set. The message names variables, never values."""


@dataclass(slots=True)
class _Token:
    access: str
    expires_at: float
    refresh: str | None
    refresh_expires_at: float | None
    margin: float


def read_secrets(auth: OidcPasswordAuth, environ: Mapping[str, str]) -> dict[str, str]:
    """The password per user sub (and the client secret under ``""``), from ``environ``."""
    secrets: dict[str, str] = {}
    missing: list[str] = []
    for sub, login in auth.users.items():
        value = environ.get(login.password_env, "")
        if value:
            secrets[sub] = value
        elif login.password_env not in missing:
            missing.append(login.password_env)
    if auth.client_secret_env is not None:
        value = environ.get(auth.client_secret_env, "")
        if value:
            secrets[""] = value
        else:
            missing.append(auth.client_secret_env)
    if missing:
        msg = (
            f"set {', '.join(missing)} in the shell (read -s / read -s -x); the target config "
            "names these variables for its passwords"
        )
        raise MissingSecretError(msg)
    return secrets


def _number(value: JsonValue, default: float | None) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float) or value <= 0:
        return default
    return float(value)


class TokenManager:
    def __init__(
        self,
        auth: OidcPasswordAuth,
        secrets: Mapping[str, str],
        post: Post,
        redactor: Redactor,
        clock: Clock,
    ) -> None:
        self._auth = auth
        self._secrets = secrets
        self._post = post
        self._redact = redactor
        self._clock = clock
        self._tokens: dict[str, _Token] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        for value in secrets.values():
            redactor.add(value)

    def knows(self, sub: str) -> bool:
        return sub in self._auth.users

    async def token_for(self, sub: str) -> str:
        """A valid access token for ``sub``, refreshed or renewed when it is about to expire."""
        if sub not in self._auth.users:
            msg = f"no login for user {sub[:64]!r} in the target config"
            raise TargetError(msg)
        lock = self._locks.setdefault(sub, asyncio.Lock())
        async with lock:
            token = self._tokens.get(sub)
            now = self._clock()
            if token is not None and now < token.expires_at - token.margin:
                return token.access
            if (
                token is not None
                and token.refresh is not None
                and (token.refresh_expires_at is None or now < token.refresh_expires_at)
            ):
                try:
                    self._tokens[sub] = await self._grant(sub, "refresh", token.refresh)
                    return self._tokens[sub].access
                except TargetError:
                    pass  # an expired or revoked refresh token: log in again with the password
            self._tokens[sub] = await self._grant(sub, "password", None)
            return self._tokens[sub].access

    async def _grant(self, sub: str, grant: str, refresh: str | None) -> _Token:
        form = {"client_id": self._auth.client_id}
        if "" in self._secrets:
            form["client_secret"] = self._secrets[""]
        if grant == "refresh" and refresh is not None:
            form |= {"grant_type": "refresh_token", "refresh_token": refresh}
        else:
            login = self._auth.users[sub]
            form |= {
                "grant_type": "password",
                "username": login.username,
                "password": self._secrets[sub],
            }
            if self._auth.scope:
                form["scope"] = self._auth.scope
        started = self._clock()
        try:
            reply = await self._post(f"login as {sub[:64]}", form)
        except TargetError as exc:
            raise TargetError(self._redact(str(exc))) from None
        if not isinstance(reply, dict):
            msg = f"login as {sub[:64]}: the token response is not a JSON object"
            raise TargetError(msg)
        access = reply.get("access_token")
        if not isinstance(access, str) or not access:
            msg = f"login as {sub[:64]}: the token response has no access_token"
            raise TargetError(msg)
        self._redact.add(access)
        new_refresh = reply.get("refresh_token")
        new_refresh = new_refresh if isinstance(new_refresh, str) and new_refresh else None
        self._redact.add(new_refresh)
        lifetime = _number(reply.get("expires_in"), DEFAULT_LIFETIME_S) or DEFAULT_LIFETIME_S
        refresh_lifetime = _number(reply.get("refresh_expires_in"), None)
        return _Token(
            access=access,
            expires_at=started + lifetime,
            refresh=new_refresh,
            refresh_expires_at=None if refresh_lifetime is None else started + refresh_lifetime,
            margin=min(self._auth.refresh_margin, lifetime / 2),
        )
