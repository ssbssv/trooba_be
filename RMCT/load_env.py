"""Load backend/.env into os.environ before Django settings are evaluated."""
from __future__ import annotations

import os
from pathlib import Path


def load_dotenv() -> None:
    env_path = Path(__file__).resolve().parent.parent / ".env"
    if not env_path.is_file():
        return
    try:
        for raw_line in env_path.read_text(encoding="utf-8").splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            key = key.strip()
            if key.startswith("$env:"):
                key = key[5:].strip()
            value = value.strip()
            # Strip unquoted inline comments: KEY=value # comment
            if value and not (value.startswith('"') or value.startswith("'")):
                hash_at = value.find(" #")
                if hash_at != -1:
                    value = value[:hash_at].rstrip()
            value = value.strip('"').strip("'")
            if key and key not in os.environ:
                os.environ[key] = value
    except OSError:
        pass
