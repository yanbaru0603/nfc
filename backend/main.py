"""FastAPIアプリの生成、ルートの登録、共通のエラー処理。

起動方法（モジュールの import 時には設定もDBも読まない）::

    .venv/bin/uvicorn backend.main:create_app --factory

SPA（frontend/dist）の配信は開発単位7で追加する。`/api` 配下の404はSPAのHTMLに
置き換えないため、追加するときは `_http_exception_handler` の `/api` 以外の分岐で
index.html を返すか、`/api` を除外した catch-all のルートを `api_router` の後に登録する。
"""

import http
import logging
import traceback
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, closing

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.responses import Response
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from backend.config import Settings, load_settings
from backend.db import connect, init_db
from backend.routes import api_router
from backend.schemas import ApiError, error_response
from backend.security import install_security, security_headers_for


logger = logging.getLogger(__name__)

# Starlette の HTTPException を変換するときの code と日本語メッセージ。
_HTTP_ERRORS: dict[int, tuple[str, str]] = {
    404: ("not_found", "指定されたAPIは見つかりません。"),
    405: ("method_not_allowed", "このメソッドは使えません。"),
}


def create_app(settings: Settings | None = None) -> FastAPI:
    """アプリを生成。settings を省略すると環境変数から読み込む。"""
    if settings is None:
        settings = load_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        # 新しいDB_PATHでも "no such table" にならないよう、起動時にスキーマを用意する。
        with closing(connect(settings.db_path)) as conn:
            init_db(conn)
        yield

    app = FastAPI(title="NFCグッズ特典受け取り PoC", lifespan=lifespan)
    app.state.settings = settings

    # add_middleware は外側に積むので、最初に登録してユーザーミドルウェアの最も内側に置く。
    app.add_middleware(InternalErrorMiddleware)
    install_security(app, settings)
    app.add_exception_handler(ApiError, _api_error_handler)
    app.add_exception_handler(StarletteHTTPException, _http_exception_handler)
    app.add_exception_handler(RequestValidationError, _validation_error_handler)
    app.add_exception_handler(Exception, _internal_error_handler)

    app.include_router(api_router)
    return app


async def _api_error_handler(request: Request, exc: Exception) -> Response:
    assert isinstance(exc, ApiError)
    return error_response(exc.status_code, exc.code, exc.message, exc.headers)


async def _http_exception_handler(request: Request, exc: Exception) -> Response:
    """ルートが見つからない404や405などを共通のエラー形式に変換。

    `/api` 以外のパスも現時点では同じ形式で返す。SPAのフォールバックはここに足せる。
    """
    assert isinstance(exc, StarletteHTTPException)
    if exc.status_code in {204, 304}:
        return Response(status_code=exc.status_code, headers=exc.headers)
    code, message = _HTTP_ERRORS.get(
        exc.status_code,
        (_status_code_name(exc.status_code), "リクエストを処理できませんでした。"),
    )
    return error_response(exc.status_code, code, message, exc.headers)


async def _validation_error_handler(request: Request, exc: Exception) -> Response:
    # 入力値をそのまま返さないよう、詳細はメッセージに含めない。
    return error_response(422, "validation_error", "入力内容が正しくありません。")


_INTERNAL_ERROR_MESSAGE = "サーバーでエラーが発生しました。時間をおいて再度お試しください。"


def _log_internal_error(method: str, exc: BaseException) -> None:
    """パスや例外のメッセージには受取トークンが含まれ得るので、メソッドと
    _describe_exception の要約だけを出し、exc_info は渡さない。"""
    logger.error("想定外のエラー（%s）\n%s", method, _describe_exception(exc))


class InternalErrorMiddleware:
    """ルートなどの想定外の例外を500の共通エラーにし、例外を再送出しない。

    Starlette の ServerErrorMiddleware はハンドラーの後に例外を再送出し、Uvicorn が
    exc_info 付き（メッセージと連鎖した例外を含む）で記録してしまうため、その内側で止める。
    ユーザーミドルウェアの最も内側に置くので、応答には共通ヘッダーが付く。
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        response_started = False

        async def send_wrapper(message: Message) -> None:
            nonlocal response_started
            if message["type"] == "http.response.start":
                response_started = True
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        except Exception as exc:
            _log_internal_error(scope["method"], exc)
            # 応答の送信後（ファイル配信の途中など）は、Uvicornが接続を閉じる。
            if not response_started:
                await error_response(500, "internal_error", _INTERNAL_ERROR_MESSAGE)(scope, receive, send)


async def _internal_error_handler(request: Request, exc: Exception) -> Response:
    """InternalErrorMiddleware より外側（セッションなどのミドルウェア）で起きた例外の予備の処理。

    ServerErrorMiddleware は共通ヘッダーのミドルウェアより外側にあるため、ここで付ける。
    この経路では Starlette が例外を再送出するので、Uvicorn のログにも残る。
    """
    _log_internal_error(request.method, exc)
    return error_response(
        500,
        "internal_error",
        _INTERNAL_ERROR_MESSAGE,
        security_headers_for(request.url.path),
    )


def _exception_type_name(exc: BaseException) -> str:
    exc_type = type(exc)
    return f"{exc_type.__module__}.{exc_type.__qualname__}"


def _describe_exception(exc: BaseException) -> str:
    """例外の型名とトレースバックのフレーム（ファイル・行・関数名）だけの要約を作る。

    メッセージとソース行は出さない。連鎖した例外（__cause__ / __context__）も型名だけにする。
    """
    lines = [_exception_type_name(exc)]
    for frame in traceback.extract_tb(exc.__traceback__):
        lines.append(f'  File "{frame.filename}", line {frame.lineno}, in {frame.name}')
    seen = {id(exc)}
    current: BaseException = exc
    while True:
        if current.__cause__ is not None:
            label, current = "原因", current.__cause__
        elif current.__context__ is not None and not current.__suppress_context__:
            label, current = "処理中に発生した例外", current.__context__
        else:
            break
        if id(current) in seen:  # 循環した連鎖で止まらないようにする。
            break
        seen.add(id(current))
        lines.append(f"{label}: {_exception_type_name(current)}")
    return "\n".join(lines)


def _status_code_name(status_code: int) -> str:
    try:
        return http.HTTPStatus(status_code).name.lower()
    except ValueError:
        return "http_error"
