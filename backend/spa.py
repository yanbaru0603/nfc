"""ビルド済みのフロントエンド（frontend/dist）の配信（spec 3.2）。

`/api` 以外のGET・HEADでルートが見つからない404を、main.py の `_http_exception_handler`
から `SpaFiles.response_for` に渡す。dist 内の実ファイルはそのまま返し、画面URL
（`/r/<token>`、`/library` など）には index.html を返す。

静的ファイルとみなすパス（dist 内のディレクトリ配下、または最後の部分に拡張子がある）で
ファイルがない場合と、不正なパスは None を返し、従来の404のまま返す。
"""

import os
import stat
from pathlib import Path

from starlette.responses import FileResponse, Response

from backend.config import PROJECT_ROOT
from backend.security import NO_STORE_HEADERS


DEFAULT_DIST_DIR = PROJECT_ROOT / "frontend/dist"
INDEX_FILE = "index.html"


class SpaFiles:
    """dist ディレクトリのファイルと、画面URL用の index.html を返す。"""

    def __init__(self, dist_dir: Path) -> None:
        self.root = Path(os.path.realpath(dist_dir))
        self.index_path = self.root / INDEX_FILE

    @classmethod
    def load(cls, dist_dir: Path | None) -> "SpaFiles | None":
        """dist の中に index.html があれば SpaFiles を作り、なければ None（SPAを配信しない）。

        index.html が dist の外を指すシンボリックリンクの場合も None にする。
        """
        if dist_dir is None:
            return None
        spa = cls(dist_dir)
        if spa._find_file([INDEX_FILE]) is None:
            return None
        return spa

    def response_for(self, path: str) -> Response | None:
        """リクエストのパス（デコード済み）に対する応答。404のままにする場合は None。"""
        parts = _path_parts(path)
        if parts is None:
            return None
        if parts:
            found = self._find_file(parts)
            if found is not None:
                file_path, stat_result = found
                if parts == [INDEX_FILE] or file_path == self.index_path:
                    return self._index_response()
                return FileResponse(file_path, stat_result=stat_result)
            if self._looks_like_static(parts):
                return None
        return self._index_response()

    def _find_file(self, parts: list[str]) -> tuple[Path, os.stat_result] | None:
        """dist 内に収まる通常のファイルを探す。シンボリックリンクで外に出るものは除く。"""
        resolved = Path(os.path.realpath(self.root.joinpath(*parts)))
        if not resolved.is_relative_to(self.root):
            return None
        try:
            stat_result = resolved.stat()
        except OSError:  # 存在しない、名前が長すぎる、途中がファイルなど。
            return None
        if not stat.S_ISREG(stat_result.st_mode):
            return None
        return resolved, stat_result

    def _looks_like_static(self, parts: list[str]) -> bool:
        """静的ファイルを指すパスか。`/assets/...` などの404を index.html に置き換えない。

        画面URLのトークン（token_urlsafe）や特典IDには `.` を含まない。
        """
        return "." in parts[-1] or (self.root / parts[0]).is_dir()

    def _index_response(self) -> Response | None:
        """index.html を返す。起動後に dist の外を指すリンクへ置き換えられた場合などは None。"""
        found = self._find_file([INDEX_FILE])
        if found is None:
            return None
        file_path, stat_result = found
        # 画面のHTMLはキャッシュさせない（spec 8）。ビルドし直した後も古い参照を残さない。
        return FileResponse(file_path, stat_result=stat_result, headers=NO_STORE_HEADERS)


def _path_parts(path: str) -> list[str] | None:
    """パスを dist からの相対の部分に分ける。親ディレクトリへの移動などは None。"""
    parts = [part for part in path.split("/") if part]
    for part in parts:
        if part in {".", ".."} or "\\" in part or "\x00" in part:
            return None
    return parts
