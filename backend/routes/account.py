"""デモログインと簡易ライブラリのAPI（spec 6.3、6.5）。

ログイン状態と戻り先は SessionMiddleware の署名付きCookieに保存する。
ライブラリの権限は取得権の状態を見ず account_id の一致で判定するため、
再発行で無効にした取得権で受け取った特典もライブラリに残る。
"""

import sqlite3
from typing import Annotated
from urllib.parse import quote

from fastapi import APIRouter, Depends, Request, Response
from pydantic import BaseModel

from backend.config import Settings
from backend.deps import get_conn, get_settings
from backend.entitlements import (
    BenefitDetail,
    BenefitNotFound,
    EntitlementNotFound,
    FileNotFound,
    account_benefit_ids,
    get_benefit_detail,
    get_benefit_for_account,
    get_file_for_account,
    get_public_entitlement,
)
from backend.files import (
    ArchiveItem,
    MediaFileError,
    archive_entry_name,
    archive_name,
    archive_response,
    inline_file_response,
    resolve_media_path,
)
from backend.routes.receive import entitlement_not_found
from backend.schemas import AccountOut, ApiError, BenefitFileOut, BenefitOut, WorkOut
from backend.security import Account, login_session, logout_session, require_account


# ログイン後の戻り先（`/r/<token>`）を保存するセッションのキー。
SESSION_RETURN_TO_KEY = "return_to"
# 戻り先がないときのログイン後の遷移先。
DEFAULT_RETURN_TO = "/library"
_RETURN_TO_PREFIX = "/r/"

router = APIRouter()


# ---- 入出力のモデル（frontend/src/api/types.ts と合わせる） ----

class AccountsOut(BaseModel):
    """types.ts の DemoAccountsResponse に対応。"""

    accounts: list[AccountOut]  # id順。


class PrepareIn(BaseModel):
    token: str


class PrepareOut(BaseModel):
    """types.ts の PrepareLoginResponse に対応。"""

    return_to: str


class LoginIn(BaseModel):
    account_id: str


class LoginOut(BaseModel):
    """types.ts の LoginResponse に対応。"""

    account: AccountOut
    return_to: str
    csrf_token: str  # ログイン時に作り直したCSRFトークン。


class LogoutOut(BaseModel):
    """types.ts の LogoutResponse に対応。"""

    csrf_token: str  # ログアウト時に作り直したCSRFトークン。


class LibraryItemOut(BaseModel):
    """ライブラリのカード1件（表紙と作品名）。"""

    work: WorkOut
    benefit: BenefitOut


class LibraryOut(BaseModel):
    """types.ts の LibraryResponse に対応。特典IDでまとめ、同じ特典は1件。"""

    items: list[LibraryItemOut]


class LibraryBenefitOut(BaseModel):
    """types.ts の LibraryBenefitResponse に対応（再生一覧）。"""

    work: WorkOut
    benefit: BenefitOut
    files: list[BenefitFileOut]  # position順。


# ---- エラー ----

def _account_not_found() -> ApiError:
    return ApiError(404, "account_not_found", "このアカウントは見つかりません")


def _benefit_not_found() -> ApiError:
    """受け取っていない特典も、存在しない特典と区別せずに返す。"""
    return ApiError(404, "benefit_not_found", "この特典は見つかりません")


def _file_not_found() -> ApiError:
    return ApiError(404, "file_not_found", "このファイルは見つかりません")


# ---- デモログイン ----

@router.get("/auth/accounts", response_model=AccountsOut)
def list_accounts(conn: Annotated[sqlite3.Connection, Depends(get_conn)]) -> AccountsOut:
    """ログイン画面で選べるデモ用アカウントの一覧。"""
    rows = conn.execute("SELECT id, name FROM accounts ORDER BY id").fetchall()
    return AccountsOut(accounts=[AccountOut(id=row["id"], name=row["name"]) for row in rows])


@router.post("/auth/prepare", response_model=PrepareOut)
def prepare_login(
    body: PrepareIn,
    request: Request,
    conn: Annotated[sqlite3.Connection, Depends(get_conn)],
) -> PrepareOut:
    """有効なトークンの特典ページ（`/r/<token>`）だけを、ログイン後の戻り先として保存する。

    任意のURLは受け付けず、戻り先はサーバーでトークンから組み立てる。
    """
    try:
        get_public_entitlement(conn, body.token)
    except EntitlementNotFound:
        raise entitlement_not_found() from None
    return_to = _RETURN_TO_PREFIX + quote(body.token, safe="")
    request.session[SESSION_RETURN_TO_KEY] = return_to
    return PrepareOut(return_to=return_to)


@router.post("/auth/login", response_model=LoginOut)
def login(
    body: LoginIn,
    request: Request,
    conn: Annotated[sqlite3.Connection, Depends(get_conn)],
) -> LoginOut:
    """選んだデモ用アカウントでログインし、保存した戻り先を一度だけ返す。"""
    row = conn.execute("SELECT id, name FROM accounts WHERE id = ?", (body.account_id,)).fetchone()
    if row is None:
        raise _account_not_found()
    csrf_token = login_session(request, row["id"])
    return_to = request.session.pop(SESSION_RETURN_TO_KEY, None)
    if not isinstance(return_to, str) or not return_to.startswith(_RETURN_TO_PREFIX):
        return_to = DEFAULT_RETURN_TO
    return LoginOut(
        account=AccountOut(id=row["id"], name=row["name"]),
        return_to=return_to,
        csrf_token=csrf_token,
    )


@router.post("/auth/logout", response_model=LogoutOut)
def logout(
    request: Request,
    account: Annotated[Account, Depends(require_account)],
) -> LogoutOut:
    """ログアウトし、作り直したCSRFトークンを返す。"""
    return LogoutOut(csrf_token=logout_session(request))


# ---- ライブラリ ----

def _require_benefit(
    benefit_id: str,
    conn: Annotated[sqlite3.Connection, Depends(get_conn)],
    account: Annotated[Account, Depends(require_account)],
) -> BenefitDetail:
    """再生一覧とZIPで同じ認可を行う。受け取っていない特典は404。"""
    try:
        return get_benefit_for_account(conn, account.id, benefit_id)
    except BenefitNotFound:
        raise _benefit_not_found() from None


@router.get("/library", response_model=LibraryOut)
def get_library(
    conn: Annotated[sqlite3.Connection, Depends(get_conn)],
    account: Annotated[Account, Depends(require_account)],
) -> LibraryOut:
    """ログイン中のアカウントが受け取った特典の一覧。同じ特典は1件にまとめる。"""
    items: list[LibraryItemOut] = []
    for benefit_id in account_benefit_ids(conn, account.id):
        try:
            detail = get_benefit_detail(conn, benefit_id)
        except BenefitNotFound:
            continue
        items.append(LibraryItemOut(
            work=WorkOut.model_validate(detail.work),
            benefit=BenefitOut.model_validate(detail.benefit),
        ))
    return LibraryOut(items=items)


# `/library/files/archive` を特典ID "files" のZIPと取り違えないよう、
# `/library/{benefit_id}/archive` より先に登録する。
@router.get("/library/files/{file_id}")
def get_library_file(
    file_id: str,
    conn: Annotated[sqlite3.Connection, Depends(get_conn)],
    account: Annotated[Account, Depends(require_account)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> Response:
    """受け取った特典のファイルだけを、inline・Range対応で配信する。"""
    try:
        file = get_file_for_account(conn, account.id, file_id)
        path = resolve_media_path(settings.media_dir, file.file_path)
    except (FileNotFound, MediaFileError):
        raise _file_not_found() from None
    return inline_file_response(
        path,
        mime_type=file.mime_type,
        filename=archive_entry_name(file.position, file.title, path),
    )


@router.get("/library/{benefit_id}", response_model=LibraryBenefitOut)
def get_library_benefit(
    detail: Annotated[BenefitDetail, Depends(_require_benefit)],
) -> LibraryBenefitOut:
    """再生一覧に表示する作品・特典・ファイル一覧。"""
    return LibraryBenefitOut(
        work=WorkOut.model_validate(detail.work),
        benefit=BenefitOut.model_validate(detail.benefit),
        files=[BenefitFileOut.model_validate(file) for file in detail.files],
    )


@router.get("/library/{benefit_id}/archive")
def get_library_archive(
    detail: Annotated[BenefitDetail, Depends(_require_benefit)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> Response:
    """全曲を同期的に無圧縮ZIPへまとめ、attachmentで配信する。"""
    items: list[ArchiveItem] = []
    try:
        for file in detail.files:
            path = resolve_media_path(settings.media_dir, file.file_path)
            items.append(ArchiveItem(path, archive_entry_name(file.position, file.title, path)))
    except MediaFileError:
        raise _file_not_found() from None
    return archive_response(items, zip_name=archive_name(detail.work.title, detail.benefit.title))
