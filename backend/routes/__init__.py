"""`/api` 配下のルーターをまとめる。

CSRFの検証と共通ヘッダーはミドルウェア（backend.security）で行うため、
各ルーターで依存関係を付ける必要はない。
"""

import sqlite3
from typing import Annotated

from fastapi import APIRouter, Depends, Request

from backend.deps import get_conn
from backend.routes import account, receive
from backend.schemas import AccountOut, ErrorResponse, SessionOut
from backend.security import current_account, ensure_csrf_token


API_PREFIX = "/api"

# OpenAPIにも共通のエラー形式を載せる。
api_router = APIRouter(
    prefix=API_PREFIX,
    responses={"4XX": {"model": ErrorResponse, "description": "エラー"}},
)


@api_router.get("/session", response_model=SessionOut)
def get_session(
    request: Request,
    conn: Annotated[sqlite3.Connection, Depends(get_conn)],
) -> SessionOut:
    """ログイン状態とCSRFトークンを取得。初回はセッションを初期化してトークンを発行する。"""
    account = current_account(request, conn)
    return SessionOut(
        account=None if account is None else AccountOut(id=account.id, name=account.name),
        csrf_token=ensure_csrf_token(request),
    )


api_router.include_router(receive.router)
api_router.include_router(account.router)
