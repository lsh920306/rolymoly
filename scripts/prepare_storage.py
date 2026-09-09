"""Initialize only Rolymoly's private Supabase schema and report safe counts."""
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from roly.core import Core
from roly.storage_config import operating_database


def main():
    try:
        target = operating_database(ROOT / ".data")
        if target != "supabase://rolymoly":
            print("운영 저장소가 Supabase로 설정되지 않았습니다. 연결 설정을 확인하세요.")
            return 2
        core = Core(target)
        with core.read_snapshot() as db:
            result = {"backend": "postgresql", "schema": core.schema,
                      "schema_version": db.execute("SELECT MAX(version) FROM _schema_migrations").fetchone()[0],
                      "tables": db.execute("SELECT COUNT(*) FROM information_schema.tables WHERE table_schema=?", (core.schema,)).fetchone()[0],
                      "accounts": db.execute("SELECT COUNT(*) FROM accounts").fetchone()[0],
                      "members": db.execute("SELECT COUNT(*) FROM members").fetchone()[0],
                      "games": db.execute("SELECT COUNT(*) FROM games").fetchone()[0]}
    except Exception:
        print("운영 저장소 준비 결과를 확인하지 못했습니다. 연결 검사 후 다시 실행하세요.")
        return 1
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
