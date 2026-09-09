"""Validated storage selection shared by the launcher, UI and database adapter.

Supabase accepts only Session pooler host names, port 5432, database postgres,
and postgres.<project-reference> users. Connection URLs are never accepted.
Runtime settings come from this project's .streamlit/secrets.toml first, or
Streamlit secrets when that file is absent. Operational storage requires a valid
Supabase connection; missing or incomplete settings fail without echoing values.

ROLYMOLY_DATABASE_TARGET overrides automatic selection. Its accepted values are
the exact marker supabase://rolymoly and a filesystem SQLite path. Relative paths
resolve against the launching process's current directory, ~ is expanded, and
absolute paths are preserved. SQLite URI/DSN syntax, other URLs, control
characters, and network share paths are rejected. A filename extension is not
required. Without this override, explicitly setting ROLYMOLY_DATA_DIR selects
the supplied data_root/rolymoly.sqlite3, independently of Supabase settings.
These explicit SQLite overrides are reserved for isolated development and tests.
"""
from collections.abc import Mapping
from dataclasses import dataclass, field
import os
from pathlib import Path
import re
import tomllib


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_SECRETS = ROOT / ".streamlit" / "secrets.toml"
SUPABASE_TARGET = "supabase://rolymoly"


class ConfigError(ValueError):
    """Only static, credential-free messages may be passed to this exception."""


@dataclass(frozen=True)
class ConnectionConfig:
    host: str
    user: str
    password: str = field(repr=False)
    port: int = 5432
    database: str = "postgres"

    def connect_kwargs(self, *, readonly=True):
        return {
            "host": self.host, "user": self.user, "password": self.password,
            "port": self.port, "dbname": self.database,
            "sslmode": "require", "connect_timeout": 10,
            "application_name": "rolymoly-test-connection-check" if readonly else "rolymoly",
            "autocommit": True, "prepare_threshold": None,
            "options": "-c default_transaction_read_only=" + ("on" if readonly else "off")
                       + " -c statement_timeout=10000",
        }


def validate_config(document):
    """Validate parsed TOML or a Cloud secrets mapping without exposing values."""
    values = document.get("supabase") if isinstance(document, Mapping) else None
    if not isinstance(values, Mapping):
        raise ConfigError("설정 파일에 [supabase] 항목이 필요합니다.")
    for name in ("host", "user", "password", "database"):
        if not isinstance(values.get(name), str) or not values[name].strip():
            raise ConfigError("host, user, password, database를 모두 입력하세요.")
    host, user = values["host"].strip(), values["user"].strip()
    if not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*\.pooler\.supabase\.com", host):
        raise ConfigError("Connect → Session pooler의 host를 입력하세요. URL 전체가 아닌 호스트 이름입니다.")
    if not re.fullmatch(r"postgres\.[a-z0-9]{8,40}", user):
        raise ConfigError("Connect 화면의 user(postgres.프로젝트참조ID)를 입력하세요.")
    if type(values.get("port")) is not int or values["port"] != 5432:
        raise ConfigError("Session pooler의 port는 숫자 5432로 입력하세요.")
    if values["database"] != "postgres":
        raise ConfigError("이 연결 검사의 database는 postgres입니다.")
    if any(char in values["password"] for char in ("\0", "\r", "\n")):
        raise ConfigError("DB 비밀번호에 줄바꿈이나 제어 문자를 넣지 마세요.")
    return ConnectionConfig(host, user, values["password"])


def _read_document(path):
    try:
        with Path(path).open("rb") as stream:
            return tomllib.load(stream)
    except FileNotFoundError:
        raise ConfigError("설정 파일이 없습니다. 먼저 --init-config를 실행하세요.") from None
    except (OSError, ValueError):
        raise ConfigError("설정 파일을 읽지 못했습니다. TOML 따옴표·형식과 접근 권한을 확인하세요.") from None


def load_config(path):
    """Load an explicit diagnostic file; this function never tries Cloud secrets."""
    return validate_config(_read_document(path))


def _cloud_document():
    try:
        import streamlit as st
        from streamlit.errors import StreamlitSecretNotFoundError
    except ImportError:
        return {}
    try:
        return st.secrets.to_dict()
    except StreamlitSecretNotFoundError as error:
        # Streamlit uses the same exception for absent and malformed secrets.
        # Only the absent-source category is an unconfigured local install.
        if getattr(error, "error_id", None) == "no-secrets-found":
            return {}
        raise ConfigError("Streamlit 연결 설정을 읽지 못했습니다. Secrets 형식을 확인하세요.") from None
    except Exception:
        raise ConfigError("Streamlit 연결 설정을 읽지 못했습니다. Secrets 형식을 확인하세요.") from None


def _runtime_document():
    try:
        local_exists = DEFAULT_SECRETS.exists()
    except OSError:
        raise ConfigError("설정 파일에 접근하지 못했습니다. 접근 권한을 확인하세요.") from None
    return _read_document(DEFAULT_SECRETS) if local_exists else _cloud_document()


def postgres_kwargs():
    """Return validated read-write psycopg arguments without opening a connection."""
    return validate_config(_runtime_document()).connect_kwargs(readonly=False)


def _sqlite_target(value):
    if not isinstance(value, str) or not value.strip():
        raise ConfigError("저장소 대상은 Supabase 지정값 또는 SQLite 파일 경로로 입력하세요.")
    value = value.strip()
    has_scheme = re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*:", value)
    windows_path = re.match(r"^[a-zA-Z]:[\\/]", value)
    if (any(ord(char) < 32 or ord(char) == 127 for char in value)
            or "://" in value or (has_scheme and not windows_path)
            or value.startswith(("\\\\", "//")) or value == ":memory:"):
        raise ConfigError("SQLite 대상은 URL이나 URI가 아닌 로컬 파일 경로로 입력하세요.")
    try:
        path = Path(value).expanduser().resolve()
        if path.is_dir():
            raise ConfigError("SQLite 대상에는 폴더가 아닌 파일 경로를 입력하세요.")
        return str(path)
    except (OSError, RuntimeError, ValueError) as error:
        if isinstance(error, ConfigError):
            raise
        raise ConfigError("SQLite 파일 경로를 확인하세요.") from None


def operating_database(data_root):
    """Select storage without connecting, creating files, or logging settings."""
    if "ROLYMOLY_DATABASE_TARGET" in os.environ:
        target = os.environ["ROLYMOLY_DATABASE_TARGET"].strip()
        if target == SUPABASE_TARGET:
            validate_config(_runtime_document())
            return SUPABASE_TARGET
        return _sqlite_target(target)
    local_path = str(Path(data_root).expanduser().resolve() / "rolymoly.sqlite3")
    if "ROLYMOLY_DATA_DIR" in os.environ:
        return local_path
    document = _runtime_document()
    if not isinstance(document, Mapping):
        raise ConfigError("연결 설정은 [supabase] 형식으로 입력하세요.")
    validate_config(document)
    return SUPABASE_TARGET
