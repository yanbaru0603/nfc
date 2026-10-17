"""DBへの取得権の登録と、タグへの書き込み・照合をつなぐ発行処理。

NFC機器はProtocol（TagWriter）で受け取り、テストでは偽物に差し替える。
取得権の操作は backend.entitlements の関数を使い、各関数が自身でトランザクションを開く。
"""

import sqlite3
from dataclasses import dataclass
from datetime import datetime
from typing import NoReturn, Protocol

import ndef

from backend.entitlements import (
    ActiveEntitlementExists,
    Entitlement,
    activate_issued,
    create_issuing,
    discard_issuing,
    find_active_by_uid,
)


# NTAG215のNDEF領域（nfcpyの capacity）。CCの容量 496バイトから、
# NDEFメッセージのTLVのタグ・長さ（3バイト長の形式で4バイト）を除いた値。
NTAG215_NDEF_CAPACITY = 492


class TagWriter(Protocol):
    """発行処理が使うタグの操作。実機では issuer.nfc_writer.NfcTagWriter。"""

    def uid(self) -> str:
        """UID（大文字の16進数、区切りなし）。"""
        ...

    def write_uri(self, url: str) -> None:
        """URIレコード1件を書き込む。失敗したら例外。"""
        ...

    def read_uri(self) -> str | None:
        """タグを読み直し、URIレコード1件のURLを返す。読めなければNone。"""
        ...


@dataclass(frozen=True)
class IssueFailure:
    """発行に失敗・中断したときの状況。CLIが復旧の手順を案内するために使う。"""

    uid: str
    token: str  # 発行中として登録したトークン。
    # タグを書き換え始めていた、または無効なトークンのURLを書き込んだ。--reissue で発行し直す。
    tag_may_be_corrupted: bool
    previous: Entitlement | None  # 同じUIDの有効な取得権（有効なまま残っている）。
    discarded: bool  # 発行中の取得権を削除できたか。
    discard_error: BaseException | None  # 削除できなかった理由。


class IssueError(Exception):
    """書き込み・照合・有効化に失敗した。詳細は failure。__cause__ が元の原因。"""

    def __init__(self, message: str, failure: IssueFailure) -> None:
        super().__init__(message)
        self.failure = failure


class IssueInterrupted(KeyboardInterrupt):
    """発行中の取得権を登録した後のCtrl+C。詳細は failure。"""

    def __init__(self, failure: IssueFailure) -> None:
        super().__init__("発行を中断しました")
        self.failure = failure


class UrlTooLong(Exception):
    """URLがNTAG215のNDEF領域に収まらない。"""


@dataclass(frozen=True)
class IssueResult:
    token: str
    uid: str
    url: str
    revoked: Entitlement | None  # 再発行で無効にした既存の取得権。新規発行ならNone。


def build_url(base_url: str, token: str) -> str:
    """タグに書き込む受け取りページのURL（<BASE_URL>/r/<token>）。"""
    return f"{base_url.rstrip('/')}/r/{token}"


def ndef_message_size(url: str) -> int:
    """URIレコード1件のNDEFメッセージのバイト数。"""
    return len(b"".join(ndef.message_encoder([ndef.UriRecord(url)])))


def check_url_fits(url: str, capacity: int = NTAG215_NDEF_CAPACITY) -> None:
    """URLがタグのNDEF領域に収まることを確認。収まらなければ UrlTooLong。"""
    size = ndef_message_size(url)
    if size > capacity:
        raise UrlTooLong(f"URLがNTAG215の容量を超えています（{size} / {capacity}バイト）: {url}")


def issue_one(
    conn: sqlite3.Connection,
    writer: TagWriter,
    *,
    benefit_id: str,
    base_url: str,
    now: datetime,
    reissue: bool = False,
) -> IssueResult:
    """1個のタグに取得権を発行する。

    同じUIDの有効な取得権があり reissue=False なら、書き込まずに ActiveEntitlementExists。
    特典がなければ BenefitNotFound。書き込み・照合・有効化に失敗したら発行中の取得権の
    削除を試みて IssueError、Ctrl+Cなら IssueInterrupted を投げる（どちらも failure に
    状況を持つ）。再発行で失敗しても、既存の取得権は有効なまま。
    """
    uid = writer.uid()
    existing = find_active_by_uid(conn, uid)
    if existing is not None and not reissue:
        raise ActiveEntitlementExists(existing)

    # 特典の確認と同じUIDの確認は create_issuing でも行う（確認の後に別の発行が入った場合）。
    token = create_issuing(conn, benefit_id=benefit_id, uid=uid, now=now, reissue=reissue)
    url = build_url(base_url, token)
    started_write = False
    try:
        check_url_fits(url)
        started_write = True
        writer.write_uri(url)
        written = writer.read_uri()
        if written != url:
            _raise_failure(
                f"読み直したURLが一致しません（読み直し: {written!r}）",
                _fail(conn, token, uid=uid, corrupted=True, previous=existing),
            )
    except (IssueError, IssueInterrupted):
        raise
    except BaseException as error:
        # 書き込みの開始前はタグを変更していない。開始後は、機器の層が判定した
        # tag_modified を使い、判定できない場合は壊れている可能性があるとみなす。
        tag_modified = getattr(error, "tag_modified", None)
        corrupted = started_write and (True if tag_modified is None else bool(tag_modified))
        failure = _fail(conn, token, uid=uid, corrupted=corrupted, previous=existing)
        _raise_failure(f"タグへの書き込み・照合に失敗しました: {error}", failure, error)

    try:
        activate_issued(conn, token, reissue=reissue)
    except BaseException as error:
        # 書き込み中に別の発行が同じUIDを有効にした場合など。タグのURLは無効なトークンを
        # 指すため、発行中の取得権を削除し、--reissue での書き直しを案内する。
        failure = _fail(conn, token, uid=uid, corrupted=True, previous=None)
        _raise_failure(f"取得権を有効にできませんでした: {error}", failure, error)
    return IssueResult(token=token, uid=uid, url=url, revoked=existing if reissue else None)


def _raise_failure(
    message: str, failure: IssueFailure, cause: BaseException | None = None,
) -> NoReturn:
    """元の原因か削除の処理がCtrl+Cなら IssueInterrupted、それ以外は IssueError を投げる。"""
    interrupted = (cause is not None and not isinstance(cause, Exception)) or (
        failure.discard_error is not None and not isinstance(failure.discard_error, Exception)
    )
    if interrupted:
        raise IssueInterrupted(failure) from cause
    raise IssueError(message, failure) from cause


def _fail(
    conn: sqlite3.Connection,
    token: str,
    *,
    uid: str,
    corrupted: bool,
    previous: Entitlement | None,
) -> IssueFailure:
    """発行中の取得権の削除を試み、失敗の状況をまとめる。

    削除がDBのエラーやCtrl+Cで失敗しても、元の原因を失わないよう例外は投げずに記録する。
    previous がNoneなら、DBから同じUIDの有効な取得権を読み直す（読めなければNone）。
    """
    discard_error: BaseException | None = None
    try:
        discard_issuing(conn, token)
    except BaseException as error:  # noqa: BLE001 - 元の失敗を優先して記録する。
        discard_error = error
        if conn.in_transaction:
            conn.rollback()
    if previous is None:
        try:
            previous = find_active_by_uid(conn, uid)
        except sqlite3.Error:
            previous = None
    return IssueFailure(
        uid=uid, token=token, tag_may_be_corrupted=corrupted, previous=previous,
        discarded=discard_error is None, discard_error=discard_error,
    )
