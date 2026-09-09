"""Start durable auction settlement before serving the Streamlit app.py entrypoint."""
import os
from pathlib import Path
import subprocess
import sys

from roly.core import Core
from roly.competition import Competition
from roly.live_auction import LiveAuction
from roly.storage_config import operating_database


def main():
    root = Path(__file__).resolve().parent.parent
    data_root = Path(os.environ.get("ROLYMOLY_DATA_DIR", root / ".data")).expanduser().resolve()
    target = operating_database(data_root)
    core = Core(target)
    live = LiveAuction(core, Competition(core))
    live.ensure_worker(persistent=True)
    child_environment = os.environ.copy()
    # The child changes cwd to the project. Freeze the data location first so
    # the deadline worker and UI cannot silently open different databases.
    child_environment["ROLYMOLY_DATA_DIR"] = str(data_root)
    child_environment["ROLYMOLY_DATABASE_TARGET"] = target
    try:
        return subprocess.call([sys.executable, "-m", "streamlit", "run", str(root / "streamlit_app.py"), *sys.argv[1:]], cwd=root, env=child_environment)
    except KeyboardInterrupt:
        return 0
    finally:
        live.stop_worker()


if __name__ == "__main__":
    raise SystemExit(main())
