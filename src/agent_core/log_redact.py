"""Keep device tokens out of the process log."""

from __future__ import annotations

import copy
import logging
import re
from typing import Any

from uvicorn.config import LOGGING_CONFIG

REDACTED = "<redacted>"

# `token=<value>` as a query parameter, also when the request target itself is
# percent-encoded (`token%3D<value>%26...`, any encoding depth) or the name is
# spelled with percent-escapes (`t%6Fken=`).
_NAME = "".join(f"(?:{c}|%(?:25)*{ord(c):X}|%(?:25)*{ord(c.upper()):X})" for c in "token")
_TOKEN = re.compile(
    rf"({_NAME})(?:(=)[^&\s\"']+|(%(?:25)*3D)(?:(?!%(?:25)*26)[^&\s\"'])+)",
    re.IGNORECASE,
)


def redact(text: str) -> str:
    return _TOKEN.sub(lambda m: f"{m.group(1)}{m.group(2) or m.group(3)}{REDACTED}", text)


def _redact_arg(arg: Any) -> Any:
    if isinstance(arg, (bool, int, float)) or arg is None:
        return arg
    text = arg if isinstance(arg, str) else str(arg)
    clean = redact(text)
    return arg if clean == text else clean


class RedactTokenFilter(logging.Filter):
    """Rewrite the `token` query value in a record's message and args."""

    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.msg, str):
            record.msg = redact(record.msg)
        if isinstance(record.args, tuple):
            record.args = tuple(_redact_arg(arg) for arg in record.args)
        elif isinstance(record.args, dict):
            record.args = {key: _redact_arg(arg) for key, arg in record.args.items()}
        return True


def log_config() -> dict[str, Any]:
    """uvicorn's logging config with the filter on every handler.

    HTTP access lines go through `uvicorn.access`, WebSocket lines through
    `uvicorn.error`; filtering at the handlers covers both.
    """
    config = copy.deepcopy(LOGGING_CONFIG)
    config["filters"] = {"redact_token": {"()": RedactTokenFilter}}
    for handler in config["handlers"].values():
        handler["filters"] = ["redact_token"]
    return config
