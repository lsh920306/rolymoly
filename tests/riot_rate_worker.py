"""Fresh process entry point for a local rate-limit test; never imports app.py."""
import argparse
import json
import math
import os
from pathlib import Path
import re
from unittest.mock import patch

from roly.core import Core
from roly.riot_api import RiotAPIError
from roly.riot_sync import DatabaseRateLimiter


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", required=True)
    parser.add_argument("--key-hash", required=True)
    parser.add_argument("--stamp", type=float, required=True)
    args = parser.parse_args()
    try:
        path = Path(args.database)
        if (not path.is_absolute() or path.suffix != ".sqlite3" or not path.is_file()
                or not re.fullmatch(r"[a-f0-9]{64}", args.key_hash) or not math.isfinite(args.stamp)):
            raise ValueError("Invalid local fixture")
        with patch("roly.riot_api._runtime_document", side_effect=AssertionError("No live settings")), \
                patch("roly.riot_api._http_get", side_effect=AssertionError("No HTTP")):
            core = Core(path)
            try:
                DatabaseRateLimiter(core, args.key_hash, clock=lambda: args.stamp).reserve("kr")
                status = "accepted"
            except RiotAPIError as error:
                status = error.code
        print(json.dumps({"status": status, "pid": os.getpid()}))
        return 0
    except Exception as error:
        # Neither SQLite driver text nor input paths/hash values are public.
        print(json.dumps({"status": "failed", "error_type": type(error).__name__}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
