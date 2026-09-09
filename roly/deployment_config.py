"""Fail-closed deployment mode and cross-database session boundaries.

Default deployments are production. A developer can explicitly choose local,
or use ROLYMOLY_DATA_DIR without a Supabase target to keep isolated local tests
away from real secrets. The launcher passes a Supabase target to its child, so
its data-directory argument cannot silently enable demonstration mode there.
"""
from collections.abc import Mapping
from dataclasses import dataclass
import os

from . import storage_config


_UNSET = object()
ENVIRONMENT_VARIABLE = "ROLYMOLY_APP_ENVIRONMENT"


@dataclass(frozen=True)
class DeploymentConfig:
    environment: str = "production"

    def __post_init__(self):
        if self.environment not in ("local", "test", "production"):
            raise storage_config.ConfigError("[app] environment는 local, test, production 중 하나로 입력하세요.")

    @property
    def allow_demo(self):
        return self.environment == "local"

    @property
    def is_test(self):
        return self.environment == "test"


def _mode(value):
    if not isinstance(value, str):
        raise storage_config.ConfigError("[app] environment는 local, test, production 중 하나로 입력하세요.")
    return DeploymentConfig(value.strip())


def load_deployment_config(document=_UNSET, *, environ=None):
    """Read only configuration; never open a database or echo supplied values.

    Precedence: explicit process mode, isolated local data-directory override,
    then [app].environment (production when omitted). Passing a document is for
    validation and bypasses the local-directory inference, but not process mode.
    """
    environment = os.environ if environ is None else environ
    if ENVIRONMENT_VARIABLE in environment:
        return _mode(environment[ENVIRONMENT_VARIABLE])
    if document is _UNSET:
        if (str(environment.get("ROLYMOLY_DATA_DIR", "")).strip()
                and str(environment.get("ROLYMOLY_DATABASE_TARGET", "")).strip() != storage_config.SUPABASE_TARGET):
            return DeploymentConfig("local")
        document = storage_config._runtime_document()
    if not isinstance(document, Mapping):
        raise storage_config.ConfigError("앱 설정은 [app] 형식으로 입력하세요.")
    values = document.get("app", {})
    if not isinstance(values, Mapping):
        raise storage_config.ConfigError("앱 설정은 [app] 형식으로 입력하세요.")
    return _mode(values.get("environment", "production"))


def require_operating_target(config, target):
    if not config.allow_demo and target != storage_config.SUPABASE_TARGET:
        raise storage_config.ConfigError("검수·운영 배포는 공용 Supabase 저장소를 사용해야 합니다.")
    return target


def clear_deployed_session(state):
    """Discard every captured form, receipt and demo credential before reuse."""
    for key in list(state):
        del state[key]
    state["space"] = "운영 공간"
    state["token"] = None
    # Do not load a token from the former context into the new shared database.
    state["auth_storage_checked"] = True


def bind_deployed_session(state, target):
    """Keep an ordinary same-database login; discard foreign/demo contexts."""
    previous = state.get("db_path")
    token = state.get("token")
    unsafe = (state.get("space") not in (None, "운영 공간")
              or previous not in (None, target)
              or bool(token and previous != target)
              or bool(token and token == state.get("demo_token")))
    if unsafe:
        clear_deployed_session(state)
    else:
        for key in list(state):
            if key.startswith("demo_"):
                del state[key]
    state["space"] = "운영 공간"
    state["db_path"] = target
    return unsafe
