"""Webと発行CLIで共有する設定を読み込む。"""

import os
import secrets
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit


PROJECT_ROOT = Path(__file__).resolve().parent.parent


@dataclass(frozen=True)
class Settings:
    db_path: Path  # SQLiteのDBファイルの絶対パス。
    media_dir: Path  # 非公開の特典ファイルを置くディレクトリの絶対パス。
    base_url: str  # NFCタグに書き込むURLの基点（末尾の / なし）。
    session_secret: str  # セッションCookieの署名に使う秘密鍵。
    retry_ttl_seconds: int  # 登録なしで受け取った後の再試行期限（秒）。
    cookie_secure: bool  # HTTPSだけでCookieを送るSecure属性の有効・無効。


def _resolve_path(value: str) -> Path:
    path = Path(value)
    if not path.is_absolute():
        path = PROJECT_ROOT / path
    return path.resolve()


def _load_session_secret() -> str:
    secret_path = PROJECT_ROOT / "instance" / "session_secret"
    secret_path.parent.mkdir(parents=True, exist_ok=True)
    if not secret_path.exists():
        # 完成したファイルを公開し、WebとCLIの同時起動でも同じ鍵を使う。
        with tempfile.NamedTemporaryFile(dir=secret_path.parent) as temporary:
            os.fchmod(temporary.fileno(), 0o600)
            temporary.write(secrets.token_urlsafe(32).encode("ascii"))
            temporary.flush()
            try:
                os.link(temporary.name, secret_path)
            except FileExistsError:
                pass
    secret_path.chmod(0o600)
    secret = secret_path.read_text(encoding="utf-8").strip()
    if not secret:
        raise ValueError("instance/session_secret が空です")
    return secret


def load_settings(env: Mapping[str, str] | None = None) -> Settings:
    """環境変数を読み、相対パスをプロジェクトルート基準で解決。"""
    values = os.environ if env is None else env
    base_url = values.get("BASE_URL", "http://localhost:5173").rstrip("/")
    try:
        parsed_url = urlsplit(base_url)
    except ValueError as error:
        raise ValueError("BASE_URL はホスト名を含む http または https のURLを指定してください") from error
    if parsed_url.scheme not in {"http", "https"} or not parsed_url.hostname:
        raise ValueError("BASE_URL はホスト名を含む http または https のURLを指定してください")
    try:
        retry_ttl_seconds = int(values.get("RETRY_TTL_SECONDS", "600"))
    except ValueError as error:
        raise ValueError("RETRY_TTL_SECONDS は1以上の整数を指定してください") from error
    if retry_ttl_seconds < 1:
        raise ValueError("RETRY_TTL_SECONDS は1以上の整数を指定してください")

    secure_value = values.get("COOKIE_SECURE")
    if secure_value is None:
        cookie_secure = parsed_url.scheme == "https"
    elif secure_value.strip().lower() in {"1", "true", "yes", "on"}:
        cookie_secure = True
    elif secure_value.strip().lower() in {"0", "false", "no", "off"}:
        cookie_secure = False
    else:
        raise ValueError("COOKIE_SECURE は true または false を指定してください")

    # SESSION_SECRETが未設定または空文字なら、ファイルに保存した鍵を使う。
    session_secret = values.get("SESSION_SECRET") or _load_session_secret()
    return Settings(
        db_path=_resolve_path(values.get("DB_PATH", "instance/app.db")),
        media_dir=_resolve_path(values.get("MEDIA_DIR", "media")),
        base_url=base_url,
        session_secret=session_secret,
        retry_ttl_seconds=retry_ttl_seconds,
        cookie_secure=cookie_secure,
    )
