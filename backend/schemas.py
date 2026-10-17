"""APIの入出力モデルと、共通のエラー形式。

フィールド名は frontend/src/api/types.ts の型と合わせる。
"""

from collections.abc import Mapping

from pydantic import BaseModel
from starlette.responses import JSONResponse


class ErrorDetail(BaseModel):
    code: str  # 画面の分岐に使う機械向けの識別子（例：csrf_invalid）。
    message: str  # 利用者に表示する日本語のメッセージ。


class ErrorResponse(BaseModel):
    """types.ts の ApiErrorBody に対応。"""

    error: ErrorDetail


class AccountOut(BaseModel):
    """types.ts の Account に対応。"""

    id: str
    name: str


class SessionOut(BaseModel):
    """types.ts の SessionResponse に対応。"""

    account: AccountOut | None
    csrf_token: str


class ApiError(Exception):
    """共通のエラー形式で返すAPIの例外。ハンドラーでJSONに変換する。"""

    def __init__(
        self,
        status_code: int,
        code: str,
        message: str,
        headers: Mapping[str, str] | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message
        self.headers = headers


def error_response(
    status_code: int,
    code: str,
    message: str,
    headers: Mapping[str, str] | None = None,
) -> JSONResponse:
    """`{"error": {"code", "message"}}` 形式の応答を作成。"""
    body = ErrorResponse(error=ErrorDetail(code=code, message=message))
    return JSONResponse(body.model_dump(), status_code=status_code, headers=headers)
