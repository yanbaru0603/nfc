"""APIの入出力モデルと、共通のエラー形式。

フィールド名は frontend/src/api/types.ts の型と合わせる。
"""

from collections.abc import Mapping

from pydantic import BaseModel, ConfigDict
from starlette.responses import JSONResponse

from backend.entitlements import ViewState


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


class WorkOut(BaseModel):
    """types.ts の Work に対応。"""

    model_config = ConfigDict(from_attributes=True)

    id: str
    title: str
    cover_path: str  # frontend/public/ 内の公開URLのパス。


class BenefitOut(BaseModel):
    """types.ts の Benefit に対応。"""

    model_config = ConfigDict(from_attributes=True)

    id: str
    title: str
    description: str


class BenefitFileOut(BaseModel):
    """types.ts の BenefitFile に対応。

    サーバー上のファイルパス（file_path）はフィールドに持たず、応答に出せないようにする。
    from_attributes で BenefitFileRow から作ると、file_path は読み捨てられる。
    """

    model_config = ConfigDict(from_attributes=True)

    id: str
    position: int
    title: str
    mime_type: str


class ReceiveOut(BaseModel):
    """types.ts の ReceiveResponse に対応（GET /api/r/<token>）。

    受け取ったアカウントや受け取り方法は持たない（spec 6.1、6.2）。
    """

    state: ViewState
    work: WorkOut
    benefit: BenefitOut
    files: list[BenefitFileOut]  # position順。


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
