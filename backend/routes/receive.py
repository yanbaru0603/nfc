"""特典情報・受取確定・登録なしダウンロードのAPI。"""

import sqlite3
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, Request, Response
from pydantic import BaseModel, Field

from backend.config import Settings
from backend.db import format_utc, utc_now
from backend.deps import get_conn, get_settings
from backend.entitlements import (
    AlreadyClaimed,
    BenefitDetail,
    BenefitNotFound,
    DownloadSession,
    EntitlementNotFound,
    FileNotFound,
    RetryExpired,
    RetryForbidden,
    authorize_retry,
    claim_for_account,
    claim_for_download,
    get_benefit_detail,
    get_file_for_retry,
    get_public_entitlement,
    is_repeatable_card,
    view_state,
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
from backend.schemas import ApiError, BenefitFileOut, BenefitOut, ReceiveOut, WorkOut
from backend.security import Account, current_account, require_account


# 登録なしで受け取った後の再試行セッションIDを保存するCookie（spec 6.4）。
# ログイン用のCookie（nfc_session）とは別に、トークンごとのPathで保持する。
RETRY_COOKIE_NAME = "nfc_retry"

_NOT_FOUND_CODE = "entitlement_not_found"
_NOT_FOUND_MESSAGE = "この特典は見つかりません"

router = APIRouter()


def retry_cookie_path(token: str) -> str:
    """再試行CookieのPath。複数のグッズの再試行セッションが干渉しないよう、トークンごとに分ける。

    api_router の接頭辞（/api）を含めた、ブラウザから見たパスを返す。
    """
    return f"/api/r/{token}"


def get_now() -> datetime:
    """判定に使う現在時刻。テストでは app.dependency_overrides で差し替える。"""
    return utc_now()


def entitlement_not_found() -> ApiError:
    """存在しない・発行中・無効なトークンの404。メッセージにトークンを含めない。"""
    return ApiError(404, _NOT_FOUND_CODE, _NOT_FOUND_MESSAGE)


@router.get("/r/{token}", response_model=ReceiveOut)
def get_receive(
    token: str,
    request: Request,
    conn: Annotated[sqlite3.Connection, Depends(get_conn)],
    account: Annotated[Account | None, Depends(current_account)],
    now: Annotated[datetime, Depends(get_now)],
) -> ReceiveOut:
    """特典の表示情報と、閲覧者向けの表示状態を取得。

    GETだけでは取得権の状態を変えないので、トランザクションを開かず読むだけにする（spec 6.1）。
    使用済みでも何の特典か分かるよう、状態にかかわらず作品・特典・ファイルを返す。
    """
    try:
        entitlement = get_public_entitlement(conn, token)
        state = view_state(
            conn,
            token,
            account_id=None if account is None else account.id,
            retry_session_id=request.cookies.get(RETRY_COOKIE_NAME),
            now=now,
        )
    except EntitlementNotFound:
        # 例外のメッセージにはトークンが含まれるので、連鎖させずに置き換える。
        raise entitlement_not_found() from None
    detail = get_benefit_detail(conn, entitlement.benefit_id)
    return ReceiveOut(
        state=state,
        repeatable=is_repeatable_card(conn, entitlement.uid),
        work=WorkOut.model_validate(detail.work),
        benefit=BenefitOut.model_validate(detail.benefit),
        files=[BenefitFileOut.model_validate(file) for file in detail.files],
    )


class ClaimAccountOut(BaseModel):
    """アカウントへの追加後、再生一覧への遷移に使う特典ID。"""

    benefit_id: str


class ClaimDownloadOut(BaseModel):
    """登録なし受取の期限。セッションIDはCookieだけに保存する。"""

    expires_at: str
    remaining_seconds: int = Field(ge=0)


class DownloadOut(ClaimDownloadOut):
    """認可済みの登録なしダウンロードページの表示情報。"""

    work: WorkOut
    benefit: BenefitOut
    files: list[BenefitFileOut]


def _remaining_seconds(session: DownloadSession, now: datetime) -> int:
    return max(0, int((session.expires_at - now).total_seconds()))


def _require_retry(
    token: str,
    request: Request,
    conn: Annotated[sqlite3.Connection, Depends(get_conn)],
    now: Annotated[datetime, Depends(get_now)],
) -> DownloadSession:
    """一覧・音声・ZIPで同じ順序の認可と期限判定を行う。"""
    try:
        return authorize_retry(conn, token, request.cookies.get(RETRY_COOKIE_NAME), now=now)
    except EntitlementNotFound:
        raise entitlement_not_found() from None
    except RetryForbidden:
        raise ApiError(403, "retry_forbidden", "このブラウザでは再試行できません") from None
    except RetryExpired:
        raise ApiError(410, "retry_expired", "再試行の期限が切れています") from None


def _retry_detail(conn: sqlite3.Connection, session: DownloadSession) -> BenefitDetail:
    try:
        entitlement = get_public_entitlement(conn, session.token)
        return get_benefit_detail(conn, entitlement.benefit_id)
    except (EntitlementNotFound, BenefitNotFound):
        raise entitlement_not_found() from None


def _file_not_found() -> ApiError:
    return ApiError(404, "file_not_found", "このファイルは見つかりません")


@router.post("/r/{token}/claim/account", response_model=ClaimAccountOut)
def claim_account(
    token: str,
    conn: Annotated[sqlite3.Connection, Depends(get_conn)],
    account: Annotated[Account, Depends(require_account)],
    now: Annotated[datetime, Depends(get_now)],
) -> ClaimAccountOut:
    """未使用の取得権をログイン中のアカウントに一度だけ追加する。"""
    try:
        entitlement = claim_for_account(conn, token, account.id, now=now)
    except EntitlementNotFound:
        raise entitlement_not_found() from None
    except AlreadyClaimed:
        raise ApiError(409, "already_claimed", "この特典はすでに受け取られています") from None
    return ClaimAccountOut(benefit_id=entitlement.benefit_id)


@router.post("/r/{token}/claim/download", response_model=ClaimDownloadOut)
def claim_download(
    token: str,
    response: Response,
    conn: Annotated[sqlite3.Connection, Depends(get_conn)],
    account: Annotated[Account | None, Depends(current_account)],
    settings: Annotated[Settings, Depends(get_settings)],
    now: Annotated[datetime, Depends(get_now)],
) -> ClaimDownloadOut:
    """登録なしの受取を確定し、期限後も1日は残る再試行Cookieを設定する。"""
    if account is not None:
        raise ApiError(403, "logged_in", "登録なしで受け取るにはログアウトしてください")
    try:
        session = claim_for_download(conn, token, now=now, ttl_seconds=settings.retry_ttl_seconds)
    except EntitlementNotFound:
        raise entitlement_not_found() from None
    except AlreadyClaimed:
        raise ApiError(409, "already_claimed", "この特典はすでに受け取られています") from None
    response.set_cookie(
        RETRY_COOKIE_NAME,
        session.id,
        max_age=settings.retry_ttl_seconds + 86400,
        path=retry_cookie_path(token),
        secure=settings.cookie_secure,
        httponly=True,
        samesite="lax",
    )
    return ClaimDownloadOut(
        expires_at=format_utc(session.expires_at),
        remaining_seconds=_remaining_seconds(session, now),
    )


@router.get("/r/{token}/download", response_model=DownloadOut)
def get_download(
    conn: Annotated[sqlite3.Connection, Depends(get_conn)],
    session: Annotated[DownloadSession, Depends(_require_retry)],
    now: Annotated[datetime, Depends(get_now)],
) -> DownloadOut:
    """同じブラウザの再試行期限と、再生可能なファイル一覧を取得する。"""
    detail = _retry_detail(conn, session)
    return DownloadOut(
        expires_at=format_utc(session.expires_at),
        remaining_seconds=_remaining_seconds(session, now),
        work=WorkOut.model_validate(detail.work),
        benefit=BenefitOut.model_validate(detail.benefit),
        files=[BenefitFileOut.model_validate(file) for file in detail.files],
    )


@router.get("/r/{token}/download/{file_id}")
def get_download_file(
    file_id: str,
    conn: Annotated[sqlite3.Connection, Depends(get_conn)],
    session: Annotated[DownloadSession, Depends(_require_retry)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> Response:
    """認可後、対象の特典に属する音声だけをRange対応で配信する。"""
    try:
        file = get_file_for_retry(conn, session.token, file_id)
        path = resolve_media_path(settings.media_dir, file.file_path)
    except (FileNotFound, MediaFileError):
        raise _file_not_found() from None
    return inline_file_response(
        path,
        mime_type=file.mime_type,
        filename=archive_entry_name(file.position, file.title, path),
    )


@router.get("/r/{token}/archive")
def get_download_archive(
    conn: Annotated[sqlite3.Connection, Depends(get_conn)],
    session: Annotated[DownloadSession, Depends(_require_retry)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> Response:
    """認可後に全曲を同期的にZIPへまとめ、attachmentで配信する。"""
    detail = _retry_detail(conn, session)
    items: list[ArchiveItem] = []
    try:
        for file in detail.files:
            path = resolve_media_path(settings.media_dir, file.file_path)
            items.append(ArchiveItem(path, archive_entry_name(file.position, file.title, path)))
        return archive_response(items, zip_name=archive_name(detail.work.title, detail.benefit.title))
    except MediaFileError:
        raise _file_not_found() from None
