"""SQLiteの接続、明示的なトランザクション、UTC日時の共通処理。"""

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

from backend.config import PROJECT_ROOT


def connect(db_path: str | Path) -> sqlite3.Connection:
    """親ディレクトリを作り、リクエストごとに使う接続を用意。"""
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path, isolation_level=None, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 5000")
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    """スキーマを冪等に作成し、WALを有効化。トランザクション外で実行。"""
    if conn.in_transaction:
        raise RuntimeError("トランザクション中はDBを初期化できません")
    conn.executescript((PROJECT_ROOT / "schema.sql").read_text(encoding="utf-8"))
    journal_mode = conn.execute("PRAGMA journal_mode = WAL").fetchone()[0]
    if journal_mode != "wal":
        raise RuntimeError(f"WALモードを有効にできません（journal_mode: {journal_mode}）")


@contextmanager
def transaction(conn: sqlite3.Connection) -> Iterator[None]:
    """書き込みをBEGIN IMMEDIATEで開始し、例外時は全変更を取り消す。

    入れ子にはできない。部品の関数はconnを受け取るだけでトランザクションを開かず、
    呼び出し側で1回だけ開く。
    """
    if conn.in_transaction:
        raise RuntimeError("トランザクションは入れ子にできません")
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield
        conn.execute("COMMIT")
    except BaseException:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise


def utc_now() -> datetime:
    """タイムゾーン付きの現在のUTC日時を取得。"""
    return datetime.now(UTC)


def format_utc(dt: datetime) -> str:
    """タイムゾーン付き日時をUTCに変換し、秒までの末尾Z形式に統一。"""
    if dt.tzinfo is None or dt.utcoffset() is None:
        raise ValueError("タイムゾーン付きの日時を指定してください")
    return dt.astimezone(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def parse_utc(s: str) -> datetime:
    """秒までの末尾Z形式だけを受け付け、UTC日時に変換。"""
    dt = datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
    if format_utc(dt) != s:
        raise ValueError("日時は YYYY-MM-DDTHH:MM:SSZ 形式で指定してください")
    return dt
