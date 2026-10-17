"""取得権の発行・確定・無効化と、再試行期限・取得権限の判定。

HTTPやNFC機器に依存しない共通処理。API側はここで投げるドメインの例外を
HTTPのステータスに対応させる。複数の文をまとめる関数は自身で1回だけ
トランザクションを開くため、呼び出し側のトランザクション内では呼ばない。
"""

import secrets
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Literal

from backend.db import format_utc, parse_utc, transaction


EntitlementStatus = Literal["issuing", "unused", "account", "download", "revoked"]
ViewState = Literal["unused", "account_owned", "download_retryable", "claimed"]

# 同じUIDで1件までに限る、有効な取得権の状態（schema.sqlの部分一意インデックスと同じ）。
ACTIVE_STATUSES: tuple[EntitlementStatus, ...] = ("unused", "account", "download")
# 利用者からは存在しないトークンとして扱う状態。
_HIDDEN_STATUSES = frozenset({"issuing", "revoked"})


class EntitlementError(Exception):
    """取得権の共通処理で投げるドメインの例外の基底。"""


class EntitlementNotFound(EntitlementError):
    """トークンが存在しない・発行中・無効。"""


class AlreadyClaimed(EntitlementError):
    """受け取り済み。同じアカウントからの再送も含む。"""


class RetryForbidden(EntitlementError):
    """再試行セッションがない、またはトークンと一致しない。"""


class RetryExpired(EntitlementError):
    """対象トークンに一致する再試行セッションの期限切れ。"""


class FileNotFound(EntitlementError):
    """ファイルが存在しない、または対象の特典に属さない。"""


class BenefitNotFound(EntitlementError):
    """特典がない、またはアカウントに権限がない。両者を区別しない。"""


@dataclass(frozen=True)
class Entitlement:
    token: str
    benefit_id: str
    uid: str
    status: EntitlementStatus
    account_id: str | None
    issued_at: datetime
    claimed_at: datetime | None


class ActiveEntitlementExists(EntitlementError):
    """発行時、同じUIDに有効な取得権がある。発行CLIが警告に使う。"""

    def __init__(self, existing: Entitlement) -> None:
        super().__init__(f"UID {existing.uid} には有効な取得権があります（{existing.status}）")
        self.existing = existing


@dataclass(frozen=True)
class DownloadSession:
    id: str  # 再試行Cookieに保存するランダムな値。
    token: str
    expires_at: datetime


@dataclass(frozen=True)
class WorkInfo:
    id: str
    title: str
    cover_path: str  # frontend/public/ 内の公開URLのパス。


@dataclass(frozen=True)
class BenefitInfo:
    id: str
    title: str
    description: str


@dataclass(frozen=True)
class BenefitFileRow:
    id: str
    benefit_id: str
    position: int
    title: str
    mime_type: str
    file_path: str  # 特典ファイル用ディレクトリからの相対パス。利用者には返さない。


@dataclass(frozen=True)
class BenefitDetail:
    work: WorkInfo
    benefit: BenefitInfo
    files: tuple[BenefitFileRow, ...]  # position順。


def _to_entitlement(row: sqlite3.Row) -> Entitlement:
    return Entitlement(
        token=row["token"],
        benefit_id=row["benefit_id"],
        uid=row["uid"],
        status=row["status"],
        account_id=row["account_id"],
        issued_at=parse_utc(row["issued_at"]),
        claimed_at=None if row["claimed_at"] is None else parse_utc(row["claimed_at"]),
    )


def _to_file(row: sqlite3.Row) -> BenefitFileRow:
    return BenefitFileRow(
        id=row["id"],
        benefit_id=row["benefit_id"],
        position=row["position"],
        title=row["title"],
        mime_type=row["mime_type"],
        file_path=row["file_path"],
    )


def _get_entitlement(conn: sqlite3.Connection, token: str) -> Entitlement | None:
    row = conn.execute("SELECT * FROM entitlements WHERE token = ?", (token,)).fetchone()
    return None if row is None else _to_entitlement(row)


def _new_session_id() -> str:
    return secrets.token_urlsafe(32)


def allow_repeat_for_uid(conn: sqlite3.Connection, uid: str) -> None:
    """ローカルCLIで読み取ったカードだけ、一度きりの受取制限から除外する。"""
    conn.execute("INSERT INTO repeatable_cards (uid) VALUES (?) ON CONFLICT(uid) DO NOTHING", (uid,))


def is_repeatable_card(conn: sqlite3.Connection, uid: str) -> bool:
    return conn.execute("SELECT 1 FROM repeatable_cards WHERE uid = ?", (uid,)).fetchone() is not None


# ---- 発行（発行CLI用） ----

def new_token() -> str:
    """受取トークンを生成（128ビット）。"""
    return secrets.token_urlsafe(16)


def find_active_by_uid(conn: sqlite3.Connection, uid: str) -> Entitlement | None:
    """同じUIDの有効な取得権（unused・account・download）を取得。"""
    row = conn.execute(
        "SELECT * FROM entitlements WHERE uid = ? AND status IN (?, ?, ?)",
        (uid, *ACTIVE_STATUSES),
    ).fetchone()
    return None if row is None else _to_entitlement(row)


def create_issuing(
    conn: sqlite3.Connection,
    *,
    benefit_id: str,
    uid: str,
    now: datetime,
    reissue: bool = False,
    token: str | None = None,
) -> str:
    """取得権を発行中として登録し、トークンを返す。

    reissue=False で同じUIDに有効な取得権があれば ActiveEntitlementExists。
    既存の取得権は activate_issued まで変更しない。特典がなければ BenefitNotFound。
    """
    token = new_token() if token is None else token
    with transaction(conn):
        if conn.execute("SELECT 1 FROM benefits WHERE id = ?", (benefit_id,)).fetchone() is None:
            raise BenefitNotFound(f"特典が見つかりません: {benefit_id}")
        if not reissue and (existing := find_active_by_uid(conn, uid)) is not None:
            raise ActiveEntitlementExists(existing)
        conn.execute(
            "INSERT INTO entitlements (token, benefit_id, uid, status, issued_at) "
            "VALUES (?, ?, ?, 'issuing', ?)",
            (token, benefit_id, uid, format_utc(now)),
        )
    return token


def activate_issued(conn: sqlite3.Connection, token: str, *, reissue: bool = False) -> None:
    """読み直しに成功した発行中の取得権を、1つのトランザクションで未使用にする。

    reissue=True なら同じUIDの有効な取得権を先に revoked にする。account だった取得権は
    account_id を残すため、受け取った人のライブラリから引き続き使える。download だった
    取得権の再試行セッションは残るが、authorize_retry が EntitlementNotFound を返す。
    対象が発行中でなければ EntitlementNotFound。
    """
    with transaction(conn):
        target = _get_entitlement(conn, token)
        if target is None or target.status != "issuing":
            raise EntitlementNotFound(f"発行中の取得権が見つかりません: {token}")
        existing = find_active_by_uid(conn, target.uid)
        if existing is not None:
            if not reissue:
                raise ActiveEntitlementExists(existing)
            conn.execute(
                "UPDATE entitlements SET status = 'revoked' "
                "WHERE uid = ? AND status IN (?, ?, ?)",
                (target.uid, *ACTIVE_STATUSES),
            )
        conn.execute(
            "UPDATE entitlements SET status = 'unused' WHERE token = ? AND status = 'issuing'",
            (token,),
        )


def discard_issuing(conn: sqlite3.Connection, token: str) -> None:
    """書き込み・照合の失敗時に、発行中の取得権だけを削除。該当がなければ何もしない。"""
    conn.execute("DELETE FROM entitlements WHERE token = ? AND status = 'issuing'", (token,))


# ---- 受け取りの確定 ----

def _raise_claim_failure(conn: sqlite3.Connection, token: str) -> None:
    """確定の更新が0件だった理由を、同じトランザクション内で判定して例外を投げる。"""
    current = _get_entitlement(conn, token)
    if current is not None and current.status in ("account", "download"):
        raise AlreadyClaimed(f"取得権はすでに受け取られています: {token}")
    raise EntitlementNotFound(f"取得権が見つかりません: {token}")


def claim_for_account(
    conn: sqlite3.Connection,
    token: str,
    account_id: str,
    *,
    now: datetime,
) -> Entitlement:
    """未使用の取得権をアカウントに追加して確定。

    通常カードは未使用の確認と更新を1つのUPDATE文で行う。受け取り済みなら同じアカウントでも
    AlreadyClaimed、存在しない・発行中・無効なら EntitlementNotFound。
    例外カードは取得権を消費せず、アカウントごとの受取記録を保存する。同じアカウントの再送も成功する。
    """
    with transaction(conn):
        current = get_public_entitlement(conn, token)
        if is_repeatable_card(conn, current.uid):
            conn.execute(
                "INSERT INTO repeatable_account_claims (token, account_id, claimed_at) VALUES (?, ?, ?) "
                "ON CONFLICT(token, account_id) DO NOTHING",
                (token, account_id, format_utc(now)),
            )
            return current
        updated = conn.execute(
            "UPDATE entitlements SET status = 'account', account_id = ?, claimed_at = ? "
            "WHERE token = ? AND status = 'unused'",
            (account_id, format_utc(now), token),
        ).rowcount
        if updated != 1:
            _raise_claim_failure(conn, token)
        entitlement = _get_entitlement(conn, token)
    assert entitlement is not None
    return entitlement


def claim_for_download(
    conn: sqlite3.Connection,
    token: str,
    *,
    now: datetime,
    ttl_seconds: int,
) -> DownloadSession:
    """登録なしで確定し、再試行セッションを登録。

    状態の更新とセッションの登録を BEGIN IMMEDIATE の1トランザクションで行い、
    どちらかが失敗したら両方とも残さない。例外は claim_for_account と同じ。
    例外カードは取得権を消費せず、受け取るたびに新しい期限付きセッションを登録する。
    """
    expires_at = format_utc(now + timedelta(seconds=ttl_seconds))
    session_id = _new_session_id()
    with transaction(conn):
        current = get_public_entitlement(conn, token)
        if not is_repeatable_card(conn, current.uid):
            updated = conn.execute(
                "UPDATE entitlements SET status = 'download', account_id = NULL, claimed_at = ? "
                "WHERE token = ? AND status = 'unused'",
                (format_utc(now), token),
            ).rowcount
            if updated != 1:
                _raise_claim_failure(conn, token)
        conn.execute(
            "INSERT INTO download_sessions (id, token, expires_at) VALUES (?, ?, ?)",
            (session_id, token, expires_at),
        )
    return DownloadSession(id=session_id, token=token, expires_at=parse_utc(expires_at))


def reset_account_benefit(conn: sqlite3.Connection, account_id: str, benefit_id: str) -> None:
    """デモ用に本人の特典を削除し、有効な取得権を同じURLのまま未使用に戻す。

    再発行で無効にした取得権は無効のまま受取先だけを外す。他人の取得権には触れない。
    呼び出し側でデモモードを確認する。
    """
    with transaction(conn):
        if not account_can_access_benefit(conn, account_id, benefit_id):
            raise BenefitNotFound("受け取った特典が見つかりません")
        conn.execute(
            "DELETE FROM repeatable_account_claims WHERE account_id = ? AND token IN "
            "(SELECT token FROM entitlements WHERE benefit_id = ?)",
            (account_id, benefit_id),
        )
        conn.execute(
            "DELETE FROM download_sessions WHERE token IN "
            "(SELECT token FROM entitlements WHERE account_id = ? AND benefit_id = ? "
            "AND uid NOT IN (SELECT uid FROM repeatable_cards))",
            (account_id, benefit_id),
        )
        conn.execute(
            "UPDATE entitlements SET status = CASE WHEN status = 'account' THEN 'unused' ELSE status END, "
            "account_id = NULL, claimed_at = NULL WHERE account_id = ? AND benefit_id = ?",
            (account_id, benefit_id),
        )


# ---- 閲覧と権限の判定 ----

def get_public_entitlement(conn: sqlite3.Connection, token: str) -> Entitlement:
    """利用者に見せてよい取得権を取得。存在しない・発行中・無効なら EntitlementNotFound。"""
    entitlement = _get_entitlement(conn, token)
    if entitlement is None or entitlement.status in _HIDDEN_STATUSES:
        raise EntitlementNotFound(f"取得権が見つかりません: {token}")
    return entitlement


def _find_valid_session(
    conn: sqlite3.Connection, token: str, retry_session_id: str | None, now: datetime,
) -> bool:
    if not retry_session_id:
        return False
    row = conn.execute(
        "SELECT 1 FROM download_sessions WHERE id = ? AND token = ? AND expires_at > ?",
        (retry_session_id, token, format_utc(now)),
    ).fetchone()
    return row is not None


def view_state(
    conn: sqlite3.Connection,
    token: str,
    *,
    account_id: str | None,
    retry_session_id: str | None,
    now: datetime,
) -> ViewState:
    """閲覧者向けの表示状態を判定。状態は変更しない。

    account_owned はログイン中の本人が追加した場合だけ。download_retryable はこのトークンに
    一致する期限内の再試行セッションがある場合だけ。それ以外の受け取り済みは claimed とし、
    受け取り方法や他人のアカウントは区別しない。無効なトークンは EntitlementNotFound。
    例外カードは常にunusedとして受取操作を表示する。
    """
    entitlement = get_public_entitlement(conn, token)
    if is_repeatable_card(conn, entitlement.uid):
        return "unused"
    if entitlement.status == "unused":
        return "unused"
    if entitlement.status == "account":
        if account_id is not None and entitlement.account_id == account_id:
            return "account_owned"
        return "claimed"
    if _find_valid_session(conn, token, retry_session_id, now):
        return "download_retryable"
    return "claimed"


def authorize_retry(
    conn: sqlite3.Connection,
    token: str,
    retry_session_id: str | None,
    *,
    now: datetime,
) -> DownloadSession:
    """再試行セッションを認可。

    判定の順番：①無効なトークンは EntitlementNotFound（期限より先に判定）、
    ②セッションIDがない・存在しない・別のトークンのもの、またはトークンが download
    でなければ RetryForbidden、③期限切れ（期限の時刻ちょうどを含む）は RetryExpired。
    例外カードは状態がdownloadでなくても、対象トークンと期限が一致するセッションを認可する。
    """
    entitlement = get_public_entitlement(conn, token)
    if not retry_session_id:
        raise RetryForbidden("再試行セッションがありません")
    row = conn.execute(
        "SELECT * FROM download_sessions WHERE id = ?", (retry_session_id,),
    ).fetchone()
    if row is None or row["token"] != token or (
        entitlement.status != "download" and not is_repeatable_card(conn, entitlement.uid)
    ):
        raise RetryForbidden("再試行セッションがこの取得権と一致しません")
    session = DownloadSession(id=row["id"], token=row["token"], expires_at=parse_utc(row["expires_at"]))
    if row["expires_at"] <= format_utc(now):
        raise RetryExpired("再試行の期限が切れています")
    return session


def account_benefit_ids(conn: sqlite3.Connection, account_id: str) -> list[str]:
    """アカウントが受け取った特典IDを重複なしで取得。

    取得権の状態は見ず、account_id の一致で判定する（無効にした取得権も含む）。
    順番は、その特典を最初に受け取った日時の新しい順。同じ日時なら特典ID順。
    """
    rows = conn.execute(
        "SELECT benefit_id FROM ("
        "SELECT benefit_id, claimed_at FROM entitlements WHERE account_id = ? "
        "UNION ALL SELECT e.benefit_id, c.claimed_at FROM repeatable_account_claims c "
        "JOIN entitlements e ON e.token = c.token WHERE c.account_id = ?) "
        "GROUP BY benefit_id ORDER BY MIN(claimed_at) DESC, benefit_id",
        (account_id, account_id),
    ).fetchall()
    return [row["benefit_id"] for row in rows]


def account_can_access_benefit(conn: sqlite3.Connection, account_id: str, benefit_id: str) -> bool:
    """アカウントが特典を受け取ったか。取得権の状態は見ない。"""
    row = conn.execute(
        "SELECT 1 FROM entitlements WHERE account_id = ? AND benefit_id = ? "
        "UNION ALL SELECT 1 FROM repeatable_account_claims c JOIN entitlements e ON e.token = c.token "
        "WHERE c.account_id = ? AND e.benefit_id = ? LIMIT 1",
        (account_id, benefit_id, account_id, benefit_id),
    ).fetchone()
    return row is not None


def get_benefit_detail(conn: sqlite3.Connection, benefit_id: str) -> BenefitDetail:
    """作品・特典・ファイル一覧（position順）を取得。特典がなければ BenefitNotFound。"""
    row = conn.execute(
        "SELECT b.id AS benefit_id, b.title AS benefit_title, b.description, "
        "w.id AS work_id, w.title AS work_title, w.cover_path "
        "FROM benefits b JOIN works w ON w.id = b.work_id WHERE b.id = ?",
        (benefit_id,),
    ).fetchone()
    if row is None:
        raise BenefitNotFound(f"特典が見つかりません: {benefit_id}")
    files = conn.execute(
        "SELECT * FROM benefit_files WHERE benefit_id = ? ORDER BY position", (benefit_id,),
    ).fetchall()
    return BenefitDetail(
        work=WorkInfo(id=row["work_id"], title=row["work_title"], cover_path=row["cover_path"]),
        benefit=BenefitInfo(id=row["benefit_id"], title=row["benefit_title"],
                            description=row["description"]),
        files=tuple(_to_file(file) for file in files),
    )


def get_benefit_for_account(
    conn: sqlite3.Connection, account_id: str, benefit_id: str,
) -> BenefitDetail:
    """アカウントが受け取った特典の詳細を取得。権限がなければ BenefitNotFound。"""
    if not account_can_access_benefit(conn, account_id, benefit_id):
        raise BenefitNotFound(f"特典が見つかりません: {benefit_id}")
    return get_benefit_detail(conn, benefit_id)


def get_file_for_account(conn: sqlite3.Connection, account_id: str, file_id: str) -> BenefitFileRow:
    """アカウントが受け取った特典のファイルを取得。所属する特典に権限がなければ FileNotFound。"""
    row = conn.execute(
        "SELECT f.* FROM benefit_files f WHERE f.id = ? AND EXISTS ("
        "SELECT 1 FROM entitlements e WHERE e.account_id = ? AND e.benefit_id = f.benefit_id "
        "UNION ALL SELECT 1 FROM repeatable_account_claims c JOIN entitlements e ON e.token = c.token "
        "WHERE c.account_id = ? AND e.benefit_id = f.benefit_id)",
        (file_id, account_id, account_id),
    ).fetchone()
    if row is None:
        raise FileNotFound(f"ファイルが見つかりません: {file_id}")
    return _to_file(row)


def get_file_for_retry(conn: sqlite3.Connection, token: str, file_id: str) -> BenefitFileRow:
    """トークンの特典に属するファイルを取得。属さなければ FileNotFound。

    再試行の認可（authorize_retry）は呼び出し側で済ませておく。
    """
    row = conn.execute(
        "SELECT f.* FROM benefit_files f JOIN entitlements e ON e.benefit_id = f.benefit_id "
        "WHERE e.token = ? AND f.id = ?",
        (token, file_id),
    ).fetchone()
    if row is None:
        raise FileNotFound(f"ファイルが見つかりません: {file_id}")
    return _to_file(row)
