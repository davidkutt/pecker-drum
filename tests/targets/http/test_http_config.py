from pathlib import Path

import pytest

from pecker_drum.observation import ALL_CHANNELS, ObservedChannel
from pecker_drum.targets.http.config import ConfigError, is_loopback, load_config, parse_config

EXAMPLE = Path(__file__).resolve().parents[3] / "docs" / "examples" / "http-target.toml"
TEXT = EXAMPLE.read_text(encoding="utf-8")


def variant(old: str, new: str) -> str:
    assert old in TEXT, old
    return TEXT.replace(old, new, 1)


def test_the_example_loads_and_observes_everything() -> None:
    config = load_config(EXAMPLE)
    assert config.label == "example-api"
    assert config.observed() == ALL_CHANNELS
    assert config.token_url() == "http://127.0.0.1:8080/oauth/token"
    assert set(config.auth.users) == {"carol", "dana", "erin"}


@pytest.mark.parametrize(
    ("old", "new"),
    [
        ('label = "example-api"', 'label = "example-api"\npassword = "hunter2-not-allowed"'),
        ("connect = 10", "connect = 10\nverify = false"),
        ('client_id = "drum-redteam"', 'client_id = "drum-redteam"\npassword = "hunter2-x"'),
        ('username = "carol"', 'username = "carol"\npassword = "hunter2-x"'),
        ('names = ["token"]', 'names = ["token"]\njs = "eval(data)"'),
        ('file_field = "file"', 'file_field = "file"\nheaders = { X = "y" }'),
    ],
)
def test_unknown_keys_are_rejected_at_every_level(old: str, new: str) -> None:
    with pytest.raises(ConfigError, match="Extra inputs are not permitted") as raised:
        parse_config(variant(old, new))
    assert "hunter2" not in str(raised.value)  # the message names keys, never values


@pytest.mark.parametrize(
    "name",
    [
        "OPENROUTER_API_KEY",
        "GITHUB_TOKEN",
        "HOME",
        "AWS_SECRET_ACCESS_KEY",
        "DRUM_OPENROUTER_MODEL",
        "drum_password",
        "DRUM_",
        "DRUM_PASS-WORD",
        "DRUMX_PASSWORD",
        "XDRUM_PASSWORD",
    ],
)
def test_secrets_come_only_from_drum_variables(name: str) -> None:
    with pytest.raises(ConfigError, match="DRUM_<NAME>"):
        parse_config(variant('"DRUM_EXAMPLE_PASSWORD_CAROL"', f'"{name}"'))
    with pytest.raises(ConfigError, match="DRUM_<NAME>"):
        parse_config(
            variant(
                '# client_secret_env = "DRUM_EXAMPLE_CLIENT_SECRET"',
                f'client_secret_env = "{name}"',
            )
        )


def test_drum_variables_are_accepted() -> None:
    config = parse_config(
        variant(
            '# client_secret_env = "DRUM_EXAMPLE_CLIENT_SECRET"', 'client_secret_env = "DRUM_X_1"'
        )
    )
    assert config.auth.client_secret_env == "DRUM_X_1"


@pytest.mark.parametrize(
    ("host", "loopback"),
    [
        ("localhost", True),
        ("LOCALHOST", True),
        ("127.0.0.1", True),
        ("127.200.3.4", True),
        ("[::1]", True),
        ("::1", True),
        ("10.0.0.5", False),
        ("192.168.1.10", False),
        ("localhost.example", False),
        ("redteam.local", False),
        ("::ffff:127.0.0.1", True),  # IPv4-mapped 127.0.0.1
    ],
)
def test_loopback_hosts(host: str, loopback: bool) -> None:
    assert is_loopback(host) is loopback


@pytest.mark.parametrize(
    "url",
    [
        "http://localhost:3000",
        "http://127.0.0.9:8080/api",
        "http://[::1]:8080",
        "https://app.example",
    ],
)
def test_plain_http_only_for_loopback(url: str) -> None:
    assert parse_config(variant('base_url = "http://127.0.0.1:8080"', f'base_url = "{url}"'))


@pytest.mark.parametrize(
    "url",
    [
        "http://app.example",
        "http://10.0.0.5:8080",
        "ftp://localhost",
        "https://user:pw@app.example",
        "https://app.example/#x",
        "https://app.example/?q=1",
        "localhost:8080",
    ],
)
def test_bad_base_urls_are_refused(url: str) -> None:
    with pytest.raises(ConfigError, match="base_url"):
        parse_config(variant('base_url = "http://127.0.0.1:8080"', f'base_url = "{url}"'))


def test_token_url_on_the_issuers_host() -> None:
    issuer = "http://localhost:8180/realms/redteam/protocol/openid-connect/token"
    config = parse_config(variant('token_url = "/oauth/token"', f'token_url = "{issuer}"'))
    assert config.token_url() == issuer
    with pytest.raises(ConfigError, match=r"auth\.token_url"):
        parse_config(variant('token_url = "/oauth/token"', 'token_url = "http://idp.example/t"'))


@pytest.mark.parametrize(
    ("old", "new", "message"),
    [
        (
            'path = "/api/conversations"\n',
            'path = "/api/{secret}"\n',
            r"unknown variable \{secret\}",
        ),
        ('content = "{message}"', 'content = "{message.__class__}"', "stray"),
        ('path = "/api/conversations"\n', 'path = "//evil.example/x"\n', "single /"),
        ('path = "/api/conversations"\n', 'path = "https://evil.example/x"\n', "single /"),
        ('names = ["done"]', 'names = ["token"]', "mapped twice"),
        ('executed = ["ok"]', "executed = []", "executed must name"),
        ('refused = ["forbidden"]', 'refused = ["ok"]', "only one of"),
        ('\nmatch = "event"', '\nmatch = "field"', "type_field"),
        ('text = "delta"', 'text = "delta[0]"', "dotted field path"),
        (
            '{ name = "collection", value = "red-team" }',
            '{ name = "a b", value = "x" }',
            "plain field",
        ),
        ('{ name = "tags", value = "{tag}" }', '{ name = "tags", value = "{message}" }', "unknown"),
    ],
)
def test_invalid_configs_are_refused(old: str, new: str, message: str) -> None:
    with pytest.raises(ConfigError, match=message):
        parse_config(variant(old, new))


def test_approval_events_need_a_deny_request() -> None:
    start = TEXT.index("[approvals]")
    end = TEXT.index("# -- fixtures")
    with pytest.raises(ConfigError, match="go together"):
        parse_config(TEXT[:start] + TEXT[end:])


def test_observed_channels_follow_the_mapped_events() -> None:
    start = TEXT.index("[events.tool_result]")
    end = TEXT.index("[events.approval]")
    config = parse_config(TEXT[:start] + TEXT[end:])
    assert config.observed() == ALL_CHANNELS - {ObservedChannel.TOOL_RESULTS}


def test_digest_ignores_limits_and_tracks_everything_else() -> None:
    base = parse_config(TEXT).digest()
    assert parse_config(TEXT).digest() == base
    assert (
        parse_config(variant("requests_per_minute = 6000", "requests_per_minute = 5")).digest()
        == base
    )
    assert parse_config(variant('label = "example-api"', 'label = "other"')).digest() != base
    renamed = variant('"DRUM_EXAMPLE_PASSWORD_CAROL"', '"DRUM_OTHER_PASSWORD"')
    assert parse_config(renamed).digest() != base


def test_missing_and_broken_files(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="not found"):
        load_config(tmp_path / "nope.toml")
    broken = tmp_path / "broken.toml"
    broken.write_text("label = ", encoding="utf-8")
    with pytest.raises(ConfigError, match="not valid TOML"):
        load_config(broken)
    missing_ca = tmp_path / "ca.toml"
    missing_ca.write_text(variant('# ca_file = "redteam-ca.pem"', 'ca_file = "ca.pem"'), "utf-8")
    with pytest.raises(ConfigError, match="ca_file"):
        load_config(missing_ca)
