"""Create the first app administrator from a private terminal, never public UI."""
import getpass
import os
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from roly.core import Core
from roly.storage_config import operating_database


def main():
    try:
        target = operating_database(Path(os.environ.get("ROLYMOLY_DATA_DIR", ROOT / ".data")))
        core = Core(target)
        if core.has_admin():
            print("이미 최초 관리자 계정이 있습니다. 기존 계정으로 로그인하세요.")
            return 1
        print("Rolymoly 앱에 로그인할 최초 관리자 계정을 만듭니다.")
        print("Supabase DB 비밀번호와 별도의 앱 로그인 비밀번호를 설정하세요.")
        username = input("로그인 아이디 (영문·숫자·._- 3~40자): ").strip()
        name = input("표시 이름 (기본 운영진): ").strip() or "운영진"
        password = getpass.getpass("앱 로그인 비밀번호 (10자 이상, 화면에 표시되지 않음): ")
        repeat = getpass.getpass("비밀번호 확인: ")
        if password != repeat:
            print("비밀번호 확인이 일치하지 않습니다. 계정을 만들지 않았습니다.")
            return 2
        core.setup_admin(username, password, name)
    except (EOFError, KeyboardInterrupt):
        print("계정 설정을 취소했습니다.")
        return 2
    except (ValueError, PermissionError) as error:
        print(str(error))
        return 2
    except Exception:
        print("계정 생성 결과를 확인하지 못했습니다. DB 연결을 확인한 후 다시 실행하세요.")
        return 3
    print("관리자 계정을 만들었습니다. 앱의 운영 공간에서 로그인하세요.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
