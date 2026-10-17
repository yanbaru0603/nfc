"""呼び出し側で認可した特典ファイルを、DBに依存せず配信する。

呼び出し側のAPIはMediaFileErrorを捕まえて共通形式のJSONエラーに変換する。
DBに登録があるのに実ファイルがない場合は、404または500として扱う。
"""

import re
import stat
import tempfile
import unicodedata
import zipfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Literal
from urllib.parse import quote

from starlette.datastructures import MutableHeaders
from starlette.responses import FileResponse, Response
from starlette.types import Message, Receive, Scope, Send


class MediaFileError(FileNotFoundError):
    """特典ディレクトリ内の通常ファイルとして解決できない。"""


def resolve_media_path(media_dir: Path, relative_path: str) -> Path:
    """親への移動や絶対パスを拒否し、リンク解決後も配信範囲を検証。"""
    if "\0" in relative_path:
        raise MediaFileError("特典ファイルのパスにNULは使えません")
    relative = Path(relative_path)
    if relative.is_absolute() or ".." in relative.parts:
        raise MediaFileError("特典ファイルには親へ移動しない相対パスを指定してください")
    try:
        root = media_dir.resolve(strict=True)
        path = (root / relative).resolve(strict=True)
        mode = path.stat().st_mode
    except (OSError, ValueError) as error:
        raise MediaFileError("特典ファイルを解決できません") from error
    if not path.is_relative_to(root) or not stat.S_ISREG(mode):
        raise MediaFileError("特典ディレクトリ内の通常ファイルを指定してください")
    return path


def safe_filename(name: str) -> str:
    """禁止文字・制御文字・書式文字・行や段落の区切りを置換し、前後を整える。"""
    sanitized = "".join(
        "_" if character in '/\\:*?"<>|' or unicodedata.category(character) in {"Cc", "Cf", "Zl", "Zp"}
        else character
        for character in name
    )
    return re.sub(r"^[\s.]+|[\s.]+$", "", sanitized) or "_"


def content_disposition(
    disposition: Literal["inline", "attachment"], filename: str, fallback: str,
) -> str:
    """安全なASCII代替名とRFC 5987のUTF-8ファイル名を併記。"""
    if disposition not in {"inline", "attachment"}:
        raise ValueError("配信方法は inline または attachment を指定してください")
    ascii_name = safe_filename("".join(
        character if character.isascii() and character != "%" else "_" for character in fallback
    ))
    encoded_name = quote(safe_filename(filename), safe="", encoding="utf-8")
    return f'{disposition}; filename="{ascii_name}"; filename*=UTF-8\'\'{encoded_name}'


class _MediaFileResponse(FileResponse):
    """Rangeエラーでも共通ヘッダーを維持し、中断時も一時ファイルを削除。"""

    def __init__(
        self, path: Path, *, media_type: str, headers: Mapping[str, str],
        cleanup_path: Path | None = None,
    ) -> None:
        super().__init__(path, media_type=media_type, headers=headers)
        self.cleanup_path = cleanup_path

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        async def send_with_headers(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = MutableHeaders(scope=message)
                for name in ("cache-control", "content-disposition", "accept-ranges"):
                    headers[name] = self.headers[name]
            await send(message)

        try:
            await super().__call__(scope, receive, send_with_headers)
        finally:
            # 外側のキャンセル中でも削除できるよう、awaitを挟まない。
            if self.cleanup_path is not None:
                self.cleanup_path.unlink(missing_ok=True)


def inline_file_response(path: Path, *, mime_type: str, filename: str) -> Response:
    """認可済み音源をinlineで配信。Range処理はStarletteに委譲。"""
    return _MediaFileResponse(path, media_type=mime_type, headers={
        "Content-Disposition": content_disposition("inline", filename, path.name),
        "Cache-Control": "no-store",
    })


def archive_entry_name(position: int, title: str, path: Path) -> str:
    """曲番号と安全な曲名に実ファイルの拡張子を付加。"""
    return safe_filename(f"{position:02d}_{safe_filename(title)}{path.suffix}")


@dataclass(frozen=True)
class ArchiveItem:
    """解決済みのファイルパスと、安全なZIP内ファイル名。"""

    path: Path
    arcname: str


def archive_response(
    items: Sequence[ArchiveItem], *, zip_name: str, fallback: str = "benefit.zip",
) -> Response:
    """要求ごとに無圧縮ZIPを一時ファイルへ作成し、送信後に削除。

    ZIPを同期的に作る。defのルートから呼ぶか、run_in_threadpoolを使うこと。
    戻り値はそのままルートから返すこと（返さずに例外にすると一時ファイルが残る）。
    arcnameがsafe_filename(arcname)と一致しなければValueErrorで拒否する。
    同じarcnameが複数あればValueErrorで拒否し、一時ファイルも作らない。
    ZIP作成失敗、Rangeエラー、送信中のキャンセルや切断でも一時ファイルを削除する。
    """
    names = [item.arcname for item in items]
    if any(name != safe_filename(name) for name in names):
        raise ValueError("ZIP内のファイル名に安全でない文字が含まれています")
    if len(names) != len(set(names)):
        raise ValueError("ZIP内のファイル名が重複しています")
    filename = safe_filename(zip_name)
    if filename.lower().endswith(".zip"):
        filename = filename[:-4] + ".zip"
    else:
        filename += ".zip"
    headers = {
        "Content-Disposition": content_disposition("attachment", filename, fallback),
        "Cache-Control": "no-store",
    }
    temporary = tempfile.NamedTemporaryFile(prefix="nfc-benefit-", suffix=".zip", delete=False)
    zip_path = Path(temporary.name)
    try:
        with temporary:
            with zipfile.ZipFile(temporary, "w", compression=zipfile.ZIP_STORED) as archive:
                for item in items:
                    archive.write(item.path, arcname=item.arcname)
        return _MediaFileResponse(
            zip_path, media_type="application/zip", headers=headers,
            cleanup_path=zip_path,
        )
    except BaseException:
        zip_path.unlink(missing_ok=True)
        raise


def archive_name(work_title: str, benefit_title: str) -> str:
    """作品名と特典名から安全なZIPファイル名を作成。"""
    return f"{safe_filename(work_title)}_{safe_filename(benefit_title)}.zip"
