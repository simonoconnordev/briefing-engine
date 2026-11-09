"""What a job handler needs, passed in explicitly so tests can substitute fakes."""

import socket
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field

import httpx
from sqlalchemy import Engine

from briefing_engine.delivery.email import Mailer
from briefing_engine.llm.scorer import PROMPT_VERSION, Scorer
from briefing_engine.llm.writer import Writer
from briefing_engine.queue.jobs import DEFAULT_LEASE_S, POLICIES, QueuePolicy


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
    writer: Writer | None = None  # None sends the ranked list with the scorer's reasons
    policies: Mapping[str, QueuePolicy] = field(default_factory=lambda: dict(POLICIES))
    lease_s: int = DEFAULT_LEASE_S
    # The worker waits for a job due within this long (a deferred fetch, a quick retry)
    # rather than stopping; anything later is left for the next run.
    wait_up_to_s: float = 120.0
    # The delivery retry schedule in seconds (§8.3): 1 min, 5 min, 30 min, 2 h, 6 h, 12 h.
    delivery_schedule_s: tuple[float, ...] = (60, 300, 1800, 7200, 21600, 43200)
