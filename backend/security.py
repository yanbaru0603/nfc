"""Cookieセッション、CSRF、ログイン状態、共通の応答ヘッダー。"""

import secrets
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from typing import Annotated

from fastapi import Depends, FastAPI, Request
from starlette.datastructures import MutableHeaders
from starlette.middleware.sessions import SessionMiddleware
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from backend.config import Settings
from backend.deps import get_conn
from backend.schemas import ApiError, error_response


SESSION_COOKIE_NAME = "nfc_session"  # ログイン用のCookie。再試行用のCookieとは別に管理する。
SESSION_ACCOUNT_KEY = "account_id"
SESSION_CSRF_KEY = "csrf_token"
CSRF_HEADER = "X-CSRF-Token"
CSRF_PROTECTED_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})

# すべての応答に付けるヘッダー（spec 8）。
COMMON_HEADERS: dict[str, str] = {
    "Referrer-Policy": "no-referrer",
    "X-Robots-Tag": "noindex",
    "X-Content-Type-Options": "nosniff",
}
NO_STORE_HEADERS: dict[str, str] = {"Cache-Control": "no-store"}


@dataclass(frozen=True)
class Account:
    id: str
    name: str


def is_api_path(path: str) -> bool:
    """`/api` とその配下のパスかどうか。`/apix` などは含めない。"""
    return path == "/api" or path.startswith("/api/")


def security_headers_for(path: str, no_store: Callable[[str], bool] = is_api_path) -> dict[str, str]:
    """パスに応じて付ける共通ヘッダーを取得。ミドルウェアの外で作る応答（500）にも使う。"""
    headers = dict(COMMON_HEADERS)
    if no_store(path):
        headers.update(NO_STORE_HEADERS)
    return headers


class SecurityHeadersMiddleware:
    """すべての応答に共通ヘッダーを付け、`no_store` に該当するパスには no-store を付ける。

    SPAのHTMLにも no-store を付ける場合は、`no_store` に判定を足すか、
    HTMLの応答で NO_STORE_HEADERS を付ける。
    """

    def __init__(self, app: ASGIApp, no_store: Callable[[str], bool] = is_api_path) -> None:
        self.app = app
        self.no_store = no_store

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        extra = security_headers_for(scope["path"], self.no_store)

        async def send_wrapper(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = MutableHeaders(scope=message)
                # ファイル配信などが付けたキャッシュ指定も上書きする。
                for name, value in extra.items():
                    headers[name] = value
            await send(message)

        await self.app(scope, receive, send_wrapper)


class CSRFMiddleware:
    """`/api` 配下の状態を変えるメソッドで、セッションのCSRFトークンとヘッダーを照合する。

    ルーターの依存関係ではなくミドルウェアにして、どのルートでも付け忘れが起きないようにする。
    SessionMiddleware の内側に置く。
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if (
            scope["type"] == "http"
            and scope["method"] in CSRF_PROTECTED_METHODS
            and is_api_path(scope["path"])
            and not _csrf_token_matches(scope)
        ):
            response = error_response(403, "csrf_invalid", "CSRFトークンが正しくありません。ページを再読み込みしてください。")
            await response(scope, receive, send)
            return
        await self.app(scope, receive, send)


def _csrf_token_matches(scope: Scope) -> bool:
    expected = scope["session"].get(SESSION_CSRF_KEY)
    if not isinstance(expected, str) or not expected:
        return False
    header_name = CSRF_HEADER.lower().encode("latin-1")
    values = [value for name, value in scope["headers"] if name == header_name]
    if len(values) != 1:
        return False
    return secrets.compare_digest(values[0], expected.encode("utf-8"))


def install_security(app: FastAPI, settings: Settings) -> None:
    """セッション・CSRF・共通ヘッダーのミドルウェアを正しい順序で登録。

    外側から 共通ヘッダー → セッション → CSRF の順になる（add_middleware は外側に積む）。
    """
    app.add_middleware(CSRFMiddleware)
    # SessionMiddleware は常に HttpOnly を付ける。Secure は設定値で切り替える。
    app.add_middleware(
        SessionMiddleware,
        secret_key=settings.session_secret,
        session_cookie=SESSION_COOKIE_NAME,
        same_site="lax",
        https_only=settings.cookie_secure,
    )
    app.add_middleware(SecurityHeadersMiddleware)


def ensure_csrf_token(request: Request) -> str:
    """セッションのCSRFトークンを取得し、なければ作成。"""
    token = request.session.get(SESSION_CSRF_KEY)
    if not isinstance(token, str) or not token:
        token = _rotate_csrf_token(request)
    return token


def _rotate_csrf_token(request: Request) -> str:
    token = secrets.token_urlsafe(32)
    request.session[SESSION_CSRF_KEY] = token
    return token


def current_account(
    request: Request,
    conn: Annotated[sqlite3.Connection, Depends(get_conn)],
) -> Account | None:
    """ログイン中のアカウントを取得。DBにないアカウントはセッションから消して未ログイン扱い。"""
    account_id = request.session.get(SESSION_ACCOUNT_KEY)
    if account_id is None:
        return None
    if isinstance(account_id, str):
        row = conn.execute("SELECT id, name FROM accounts WHERE id = ?", (account_id,)).fetchone()
        if row is not None:
            return Account(id=row["id"], name=row["name"])
    request.session.pop(SESSION_ACCOUNT_KEY, None)
    return None


def require_account(
    request: Request,
    conn: Annotated[sqlite3.Connection, Depends(get_conn)],
) -> Account:
    """ログイン中のアカウントを取得し、未ログインなら401で拒否。"""
    account = current_account(request, conn)
    if account is None:
        raise ApiError(401, "login_required", "ログインが必要です。")
    return account


def login_session(request: Request, account_id: str) -> str:
    """セッションをログイン状態にし、作り直したCSRFトークンを返す。

    ログイン前のトークンを使い続けさせないよう、CSRFトークンを作り直す。
    戻り先などほかのセッションの値は残すので、呼び出し側で必要に応じて消す。
    """
    request.session[SESSION_ACCOUNT_KEY] = account_id
    return _rotate_csrf_token(request)


def logout_session(request: Request) -> str:
    """セッションからログイン状態を消し、作り直したCSRFトークンを返す。"""
    request.session.pop(SESSION_ACCOUNT_KEY, None)
    return _rotate_csrf_token(request)
