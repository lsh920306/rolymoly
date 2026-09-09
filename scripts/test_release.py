"""Run local regression tests and record the exact source revision tested."""
from datetime import datetime, timezone
import argparse
import hashlib
import json
import os
import re
from pathlib import Path
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[1]


def manifest():
    paths = [ROOT / "app.py", ROOT / "streamlit_app.py"]
    for name in ("roly", "app_pages", "tests", "scripts"):
        paths.extend((ROOT / name).glob("*.py"))
    paths.extend(path for path in (ROOT / "static").iterdir() if path.is_file())
    paths.extend([ROOT / ".streamlit" / "config.toml", ROOT / ".streamlit" / "secrets.toml.example", ROOT / "requirements.txt"])
    return {path.relative_to(ROOT).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in sorted(paths)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--label", default="release-tests", help="Separate artifact name for this verification run")
    args = parser.parse_args()
    if not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_-]{0,79}", args.label):
        parser.error("label must contain only letters, numbers, underscores, or hyphens")
    artifacts = ROOT / "test-artifacts"
    artifacts.mkdir(exist_ok=True)
    before = manifest()
    started = datetime.now(timezone.utc).isoformat()
    clock = time.monotonic()
    with (artifacts / f"{args.label}.log").open("w", encoding="utf-8") as output:
        result = subprocess.run(
            [sys.executable, "-B", "-X", "utf8", "-m", "unittest", "discover", "-s", "tests", "-v"],
            cwd=ROOT, stdout=output, stderr=subprocess.STDOUT,
            env={**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONDONTWRITEBYTECODE": "1"},
            check=False,
        )
    after = manifest()
    changed = [name for name in sorted(before.keys() | after.keys()) if before.get(name) != after.get(name)]
    report = {"started_at": started, "finished_at": datetime.now(timezone.utc).isoformat(),
              "elapsed_seconds": round(time.monotonic() - clock, 3),
              "returncode": result.returncode, "changed_during_tests": changed,
              "source_sha256": after}
    (artifacts / f"{args.label}-report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps({key: value for key, value in report.items() if key != "source_sha256"}))
    return result.returncode or (1 if changed else 0)


if __name__ == "__main__":
    raise SystemExit(main())
