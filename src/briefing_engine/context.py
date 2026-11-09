"""What a job handler needs, passed in explicitly so tests can substitute fakes."""

import socket
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field

import httpx
from sqlalchemy import Engine

from briefing_engine.delivery.email import Mailer
from briefing_engine.llm.scorer import PROMPT_VERSION, Scorer


def default_worker_id() -> str:
    return f"{socket.gethostname()}:{uuid.uuid4().hex[:8]}"


@dataclass
class Context:
    engine: Engine
    scorer: Scorer
    mailer: Mailer
    http_client: Callable[[], httpx.Client]
    worker_id: str = field(default_factory=default_worker_id)
    prompt_version: int = PROMPT_VERSION
    web_base_url: str | None = None  # where `briefing web` runs, for the email's marking link
