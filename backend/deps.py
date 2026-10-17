"""ルートで共通に使うFastAPIの依存関係（設定とDB接続）。"""

import sqlite3
from collections.abc import Iterator

from fastapi import Request

from backend.config import Settings
from backend.db import connect


def get_settings(request: Request) -> Settings:
    """create_app で app.state に保持した設定を取得。"""
    return request.app.state.settings


def get_conn(request: Request) -> Iterator[sqlite3.Connection]:
    """リクエストごとにDBへ接続し、応答後に閉じる。

    同じリクエスト内の依存関係では同じ接続を共有する（FastAPIの依存キャッシュ）。
    トランザクションは開かないので、書き込む側で transaction(conn) を1回だけ開く。
    """
    conn = connect(get_settings(request).db_path)
    try:
        yield conn
    finally:
        conn.close()
