"""Run inside the Render service shell; uses SQLite's consistent online backup API."""
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from src.config import load_settings


def main():
    path = load_settings().database_path
    dest = path.parent / "backups" / (datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + ".sqlite3")
    dest.parent.mkdir(mode=0o700, exist_ok=True)
    with sqlite3.connect(path) as source, sqlite3.connect(dest) as target:
        source.backup(target)
    Path(dest).chmod(0o600)
    print(f"Consistent database backup created: {dest}")


if __name__ == "__main__":
    main()
