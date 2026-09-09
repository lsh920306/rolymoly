"""Prepare local secrets and check Supabase connectivity without changing data.

This standalone diagnostic checks connectivity without applying migrations.
Never print credentials, connection strings, or raw driver/config exceptions.
"""
import argparse
from contextlib import suppress
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
if __package__ in (None, ""):
    sys.path.insert(0, str(ROOT))

from roly.storage_config import ConfigError, ConnectionConfig, load_config

DEFAULT_SECRETS = ROOT / ".streamlit" / "secrets.toml"
TEMPLATE = ROOT / ".streamlit" / "secrets.toml.example"


def init_config(path):
    """Exclusive creation preserves any settings already entered by the user."""
    template = TEMPLATE.read_text(encoding="utf-8")
    template = template.replace(
        "# 이 예시 파일에는 실제 비밀번호를 넣지 마세요.",
        "# 실제 입력 파일입니다. 아래 password에 프로젝트 DB 비밀번호를 입력하세요.",
    ).replace(
        "# 실제 입력 파일: .streamlit/secrets.toml (Git/배포 ZIP 제외)",
        "# 이 파일은 Git/배포 ZIP에서 제외됩니다. 비밀번호를 채팅에 보내지 않아도 됩니다.",
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with path.open("x", encoding="utf-8") as stream:
            stream.write(template)
    except FileExistsError:
        return False
    return True


def inspect_connection(config, connect):
    """Read server capabilities in a read-only transaction, then always close."""
    connection = connect(**config.connect_kwargs())
    try:
        connection.execute("BEGIN TRANSACTION READ ONLY")
        row = connection.execute(
            "SELECT current_setting('server_version_num')::integer, "
            "current_setting('transaction_read_only'), "
            "EXISTS (SELECT 1 FROM pg_available_extensions WHERE name = 'pg_cron'), "
            "EXISTS (SELECT 1 FROM pg_extension WHERE extname = 'pg_cron')"
        ).fetchone()
        if row is None or row[1] != "on":
            raise RuntimeError("Read-only diagnostic could not be confirmed")
        return {"postgres_major": int(row[0]) // 10000,
                "read_only": True, "cron_available": bool(row[2]),
                "cron_installed": bool(row[3])}
    finally:
        # A SQL error may leave the transaction aborted. Never commit diagnostics.
        with suppress(Exception):
            connection.execute("ROLLBACK")
        connection.close()


def connection_error_hint(error):
    # Driver text can include connection details. Report known categories only.
    state = getattr(error, "sqlstate", None)
    if state in ("28P01", "28000"):
        return "인증 실패: Connect의 user와 프로젝트 DB 비밀번호를 확인하세요."
    if state == "53300":
        return "연결 수 한도에 도달했습니다. 사용하지 않는 연결을 닫고 다시 실행하세요."
    if state == "57014":
        return "조회 제한 시간을 초과했습니다. 프로젝트 상태를 확인한 뒤 다시 실행하세요."
    return "연결을 확인하지 못했습니다. 프로젝트 실행 상태, Session pooler 정보, 네트워크를 확인하세요."


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--secrets-file", type=Path, default=DEFAULT_SECRETS)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--init-config", action="store_true", help="빈 설정 파일 준비; 기존 파일 보존")
    mode.add_argument("--check-config", action="store_true", help="입력 형식만 확인; DB 접속 없음")
    args = parser.parse_args(argv)
    if args.init_config:
        try:
            created = init_config(args.secrets_file)
        except OSError:
            print("설정 파일을 준비하지 못했습니다. 폴더 접근 권한을 확인하세요.", file=sys.stderr)
            return 2
        print("빈 설정 파일을 준비했습니다. Supabase 연결 정보를 입력하세요." if created
              else "기존 설정 파일을 보존했습니다. 덮어쓰지 않았습니다.")
        return 0
    try:
        config = load_config(args.secrets_file)
    except ConfigError as error:
        print(str(error), file=sys.stderr)
        return 2
    if args.check_config:
        print("설정 형식 확인 완료. 실제 DB 연결은 아직 검사하지 않았습니다.")
        return 0
    try:
        import psycopg
    except ImportError:
        print("DB 드라이버가 없습니다. pip install -r requirements.txt를 실행하세요.", file=sys.stderr)
        return 3
    try:
        result = inspect_connection(config, psycopg.connect)
    except Exception as error:
        print(connection_error_hint(error), file=sys.stderr)
        return 4
    print(f"연결 성공: PostgreSQL {result['postgres_major']}, 읽기 전용 조회 완료.")
    print(f"pg_cron: 사용 가능={result['cron_available']}, 설치됨={result['cron_installed']}")
    print("연결 검사에서는 테이블·데이터를 변경하지 않았습니다.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
