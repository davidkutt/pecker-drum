"""HTTP target adapter: attack an API target from the outside (M2, ADR 0009)."""

from pecker_drum.targets.http.auth import MissingSecretError
from pecker_drum.targets.http.config import ConfigError, HttpTargetConfig, load_config, parse_config
from pecker_drum.targets.http.target import (
    TAG_PREFIX,
    HttpTarget,
    LeftoversError,
    RemoteDocument,
    clean_up_run,
)

__all__ = [
    "TAG_PREFIX",
    "ConfigError",
    "HttpTarget",
    "HttpTargetConfig",
    "LeftoversError",
    "MissingSecretError",
    "RemoteDocument",
    "clean_up_run",
    "load_config",
    "parse_config",
]
