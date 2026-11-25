from __future__ import annotations

import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

DEFAULT_GDRIVE_CANDIDATE_DIRS: tuple[str, ...] = (
    "G:/My Drive/OmniShoppingBackups",
    "G:/Мій диск/OmniShoppingBackups",
    "/mnt/g/My Drive/OmniShoppingBackups",
    "~/Google Drive/My Drive/OmniShoppingBackups",
)


def _resolve_backup_dir(backup_dir: Optional[str] = None) -> Optional[Path]:
    if backup_dir:
        return Path(backup_dir).expanduser()

    env_dir = os.environ.get("OMNI_DRIVE_BACKUP_DIR", "").strip()
    if env_dir:
        return Path(env_dir).expanduser()

    for candidate in DEFAULT_GDRIVE_CANDIDATE_DIRS:
        p = Path(candidate).expanduser()
        if p.parent.exists():
            return p

    return None


def backup_sqlite_to_drive(
    db_path: str | Path,
    backup_dir: Optional[str] = None,
) -> Optional[Path]:
    """
    Atomically backs up `db_path` using sqlite3.Connection.backup() into a Google Drive
    sync folder (or explicit `backup_dir`). Returns destination Path or None if source
    does not exist or no Google Drive mount/backup_dir is configured.
    """
    src_path = Path(db_path)
    if str(db_path) == ":memory:" or not src_path.exists():
        return None

    target_dir = _resolve_backup_dir(backup_dir)
    if target_dir is None:
        return None

    target_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    stem = src_path.stem
    tmp_path = target_dir / f".{stem}_backup_{ts}.tmp.db"
    final_path = target_dir / f"{stem}_backup_{ts}.db"

    src_conn = sqlite3.connect(str(src_path))
    dst_conn = sqlite3.connect(str(tmp_path))
    try:
        with dst_conn:
            src_conn.backup(dst_conn)
    finally:
        dst_conn.close()
        src_conn.close()

    tmp_path.replace(final_path)
    return final_path
