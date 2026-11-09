"""Logging for the long-running processes (§12.1).

`LOG_FORMAT=json` writes one JSON object per line with the request id, which is what
CloudWatch Logs Insights queries; the default is plain text for a terminal. Either way,
anything that looks like an API key is redacted before it is written.
"""

import json
import logging
from datetime import UTC, datetime

from briefing_engine.api.keys import RedactKeys
from briefing_engine.api.problems import request_id_var


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        entry = {
            "ts": datetime.fromtimestamp(record.created, UTC).isoformat(timespec="milliseconds"),
            "level": record.levelname.lower(),
            "logger": record.name,
            "message": record.getMessage(),
            "request_id": request_id_var.get(),
        }
        if record.exc_info:
            entry["error"] = self.formatException(record.exc_info)
        return json.dumps(entry, ensure_ascii=False)


def configure(level: int, fmt: str = "text") -> None:
    handler = logging.StreamHandler()
    handler.setFormatter(
        JsonFormatter()
        if fmt == "json"
        else logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    )
    handler.addFilter(RedactKeys())
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level)
    # httpx logs every request URL at INFO; feed URLs are fine, but keep the noise down.
    logging.getLogger("httpx").setLevel(logging.WARNING)
