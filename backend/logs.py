"""Uvicornのアクセスログから受取トークンを伏せる（spec 8）。

アクセスログは `uvicorn.access` のロガーに、引数 (client_addr, method, path, http_version,
status_code) で記録される。パスの `/r/<token>`（`/api/r/<token>` も含む）を `/r/***` にする。
"""

import copy
import logging
import re
from typing import Any

from uvicorn.config import LOGGING_CONFIG


ACCESS_LOGGER_NAME = "uvicorn.access"
REDACTED = "/r/***"

# frontend/vite-logger.ts と同じく、`/r/` に続くトークン（token_urlsafe の文字）を伏せる。
# パスはURLエンコード済みなので `%` も含め、クエリの手前までをトークンとみなす。
_TOKEN_PATTERN = re.compile(r"/r/[^/?#\s\"]+")


def redact_receive_tokens(text: str) -> str:
    """文字列中の `/r/<token>` を `/r/***` に置き換える。"""
    return _TOKEN_PATTERN.sub(REDACTED, text)


# exc_info を文字列にするための書式。Formatter.format と同じ formatException を使う。
_EXCEPTION_FORMATTER = logging.Formatter()


class RedactTokenFilter(logging.Filter):
    """ログのメッセージ・引数・トレースバックに含まれる受取トークンを伏せる。記録は止めない。

    AccessFormatter は引数を分解して書式化するため、引数の個数と型は変えない。
    例外（exc_info）は Formatter と同じ形式の文字列にして伏せ、exc_text に入れて exc_info を消す。
    Formatter.format は exc_text があればそれを使い、exc_info を書式化し直さない。
    """

    def filter(self, record: logging.LogRecord) -> bool:
        if record.exc_info:
            record.exc_text = _EXCEPTION_FORMATTER.formatException(record.exc_info)
            record.exc_info = None
        if record.exc_text:
            record.exc_text = redact_receive_tokens(record.exc_text)
        if record.stack_info:
            record.stack_info = redact_receive_tokens(record.stack_info)
        if isinstance(record.msg, str):
            record.msg = redact_receive_tokens(record.msg)
        if isinstance(record.args, tuple):
            record.args = tuple(
                redact_receive_tokens(arg) if isinstance(arg, str) else arg for arg in record.args
            )
        elif isinstance(record.args, dict):
            record.args = {
                key: redact_receive_tokens(value) if isinstance(value, str) else value
                for key, value in record.args.items()
            }
        return True


def uvicorn_log_config() -> dict[str, Any]:
    """Uvicornの既定のログ設定に、アクセスログのハンドラーの RedactTokenFilter を足したもの。

    ロガーではなくハンドラーに付け、ほかのロガーから伝わった記録も伏せる。
    """
    config = copy.deepcopy(LOGGING_CONFIG)
    config.setdefault("filters", {})["redact_token"] = {"()": RedactTokenFilter}
    for handler in config["handlers"].values():
        handler.setdefault("filters", []).append("redact_token")
    return config
