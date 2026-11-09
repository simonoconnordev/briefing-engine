"""SQLAlchemy Core definitions of the tables, matching the migrations.

The migrations are the source of truth for the schema. These definitions let code build
queries with checked column names, and an integration test compares them with the schema
the migrations produce, so the two cannot drift apart.
"""

from sqlalchemy import (
    REAL,
    BigInteger,
    Boolean,
    CheckConstraint,
    Column,
    Date,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Identity,
    Index,
    Integer,
    LargeBinary,
    MetaData,
    PrimaryKeyConstraint,
    SmallInteger,
    Table,
    Text,
    Time,
    UniqueConstraint,
    Uuid,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, CITEXT, JSONB

metadata = MetaData()

TS = DateTime(timezone=True)

schema_meta = Table(
    "schema_meta",
    metadata,
    Column("one_row", Boolean, primary_key=True, server_default=text("true")),
    Column("schema_version", Integer, nullable=False),
    CheckConstraint("one_row"),
)

users = Table(
    "users",
    metadata,
    Column("id", Uuid, primary_key=True),
    Column("email", CITEXT, nullable=False, unique=True),
    Column("display_name", Text, nullable=False),
    Column("timezone", Text, nullable=False),
    Column("send_time", Time, nullable=False),
    Column("monthly_budget_micros", BigInteger, nullable=False, server_default="5000000"),
    Column("email_enabled", Boolean, nullable=False, server_default=text("true")),
    Column("created_at", TS, nullable=False, server_default=text("now()")),
    Column("disabled_at", TS),
)

sources = Table(
    "sources",
    metadata,
    Column("id", BigInteger, Identity(always=True), primary_key=True),
    Column("kind", Text, nullable=False),
    Column("name", Text, nullable=False),
    Column("url", Text, nullable=False),
    Column("url_normalised", Text, nullable=False, unique=True),
    Column("host", Text, nullable=False),
    Column("extract_full_text", Boolean, nullable=False, server_default=text("false")),
    Column("status", Text, nullable=False, server_default="active"),
    Column("created_at", TS, nullable=False, server_default=text("now()")),
    Column("exclude_categories", ARRAY(Text), nullable=False, server_default="{}"),
    Column("consecutive_failures", Integer, nullable=False, server_default="0"),
)

hosts = Table(
    "hosts",
    metadata,
    Column("host", Text, primary_key=True),
    Column("min_interval_ms", Integer, nullable=False, server_default="2000"),
    Column("next_allowed_at", TS, nullable=False, server_default=text("now()")),
    Column("blocked_until", TS),
)

fetch_log = Table(
    "fetch_log",
    metadata,
    Column("id", BigInteger, Identity(always=True), primary_key=True),
    Column("source_id", BigInteger, ForeignKey("sources.id"), nullable=False),
    Column("job_id", BigInteger),
    Column("host", Text, nullable=False),
    Column("started_at", TS, nullable=False),
    Column("finished_at", TS, nullable=False, server_default=text("now()")),
    Column("status_code", Integer),
    Column("outcome", Text, nullable=False),
    Column("error", Text),
    Column("bytes", Integer),
    Index("fetch_log_host", "host", "started_at"),
    Index("fetch_log_source", "source_id", "started_at"),
)

articles = Table(
    "articles",
    metadata,
    Column("id", BigInteger, Identity(always=True), primary_key=True),
    Column("url_canonical", Text, nullable=False),
    Column("url_hash", LargeBinary, nullable=False, unique=True),
    Column("content_hash", LargeBinary, nullable=False),
    Column("input_hash", LargeBinary, nullable=False),
    Column("title", Text, nullable=False),
    Column("author", Text),
    Column("published_at", TS),
    Column("first_seen_at", TS, nullable=False, server_default=text("now()")),
    Column("last_seen_at", TS, nullable=False, server_default=text("now()")),
    Column("summary_text", Text, nullable=False, server_default=""),
    Index("articles_content_hash", "content_hash"),
    Index("articles_first_seen", "first_seen_at", "id"),
)

article_sources = Table(
    "article_sources",
    metadata,
    Column("article_id", BigInteger, ForeignKey("articles.id"), primary_key=True),
    Column("source_id", BigInteger, ForeignKey("sources.id"), primary_key=True),
    Column("first_seen_at", TS, nullable=False, server_default=text("now()")),
)

profiles = Table(
    "profiles",
    metadata,
    Column("id", Uuid, primary_key=True),
    Column("user_id", Uuid, ForeignKey("users.id"), nullable=False),
    Column("name", Text, nullable=False),
    Column("current_version", Integer, nullable=False),
    Column("threshold", REAL, nullable=False),
    Column("max_items", Integer, nullable=False, server_default="12"),
    Column("created_at", TS, nullable=False, server_default=text("now()")),
    Column("max_age_days", Integer, nullable=False, server_default="7"),
    UniqueConstraint("user_id", "name"),
    UniqueConstraint("id", "user_id"),
)

profile_versions = Table(
    "profile_versions",
    metadata,
    Column("profile_id", Uuid, ForeignKey("profiles.id"), nullable=False),
    Column("version", Integer, nullable=False),
    Column("rubric", JSONB, nullable=False),
    Column("rubric_hash", LargeBinary, nullable=False),
    Column("created_at", TS, nullable=False, server_default=text("now()")),
    PrimaryKeyConstraint("profile_id", "version"),
)

subscriptions = Table(
    "subscriptions",
    metadata,
    Column("id", Uuid, primary_key=True),
    Column("user_id", Uuid, nullable=False),
    Column("source_id", BigInteger, ForeignKey("sources.id"), nullable=False),
    Column("profile_id", Uuid, nullable=False),
    Column("weight", REAL, nullable=False, server_default="1.0"),
    Column("created_at", TS, nullable=False, server_default=text("now()")),
    UniqueConstraint("profile_id", "source_id"),
    ForeignKeyConstraint(["profile_id", "user_id"], ["profiles.id", "profiles.user_id"]),
    Index("subscriptions_source", "source_id"),
)

llm_calls = Table(
    "llm_calls",
    metadata,
    Column("id", BigInteger, Identity(always=True), primary_key=True),
    Column("user_id", Uuid, ForeignKey("users.id"), nullable=False),
    Column("purpose", Text, nullable=False),
    Column("model", Text, nullable=False),
    Column("request_id", Text),
    Column("input_tokens", Integer, nullable=False, server_default="0"),
    Column("output_tokens", Integer, nullable=False, server_default="0"),
    Column("cache_read_tokens", Integer, nullable=False, server_default="0"),
    Column("cache_write_tokens", Integer, nullable=False, server_default="0"),
    Column("cost_micros", BigInteger, nullable=False),
    Column("estimate_micros", BigInteger, nullable=False),
    Column("latency_ms", Integer, nullable=False),
    Column("outcome", Text, nullable=False),
    Column("created_at", TS, nullable=False, server_default=text("now()")),
    Index("llm_calls_user_created", "user_id", "created_at"),
)

llm_scores = Table(
    "llm_scores",
    metadata,
    Column("input_hash", LargeBinary, nullable=False),
    Column("rubric_hash", LargeBinary, nullable=False),
    Column("model", Text, nullable=False),
    Column("prompt_version", Integer, nullable=False),
    Column("score", SmallInteger, nullable=False),
    Column("reason", Text, nullable=False),
    Column("topics", ARRAY(Text), nullable=False, server_default="{}"),
    Column("method", Text, nullable=False),
    Column("llm_call_id", BigInteger, ForeignKey("llm_calls.id"), nullable=False),
    Column("created_at", TS, nullable=False, server_default=text("now()")),
    PrimaryKeyConstraint("input_hash", "rubric_hash", "model", "prompt_version"),
)

briefings = Table(
    "briefings",
    metadata,
    Column("id", Uuid, primary_key=True),
    Column("user_id", Uuid, ForeignKey("users.id"), nullable=False),
    Column("profile_id", Uuid, nullable=False),
    Column("profile_version", Integer, nullable=False),
    Column("briefing_date", Date, nullable=False),
    Column("revision", Integer, nullable=False, server_default="1"),
    Column("status", Text, nullable=False),
    Column("writeup", JSONB),
    Column("cost_micros", BigInteger, nullable=False, server_default="0"),
    Column("created_at", TS, nullable=False, server_default=text("now()")),
    Column("ready_at", TS),
    UniqueConstraint("user_id", "profile_id", "briefing_date"),
    ForeignKeyConstraint(
        ["profile_id", "profile_version"],
        ["profile_versions.profile_id", "profile_versions.version"],
    ),
    Index("briefings_user_date", "user_id", "briefing_date", "id"),
)

briefing_items = Table(
    "briefing_items",
    metadata,
    Column("briefing_id", Uuid, ForeignKey("briefings.id", ondelete="CASCADE"), primary_key=True),
    Column("article_id", BigInteger, ForeignKey("articles.id"), primary_key=True),
    Column("rank", SmallInteger, nullable=False),
    Column("score", SmallInteger, nullable=False),
    Column("blurb", Text, nullable=False),
    Column("group_key", Text),
    UniqueConstraint("briefing_id", "rank"),
)

deliveries = Table(
    "deliveries",
    metadata,
    Column("id", Uuid, primary_key=True),
    Column("briefing_id", Uuid, ForeignKey("briefings.id", ondelete="CASCADE"), nullable=False),
    Column("briefing_revision", Integer, nullable=False),
    Column("channel", Text, nullable=False),
    Column("endpoint_id", Uuid),
    Column("status", Text, nullable=False, server_default="pending"),
    Column("attempts", Integer, nullable=False, server_default="0"),
    Column("next_attempt_at", TS, nullable=False, server_default=text("now()")),
    Column("last_error", Text),
    Column("message_id", Text),
    Column("created_at", TS, nullable=False, server_default=text("now()")),
    Column("updated_at", TS, nullable=False, server_default=text("now()")),
    Column("succeeded_at", TS),
    UniqueConstraint(
        "briefing_id",
        "briefing_revision",
        "channel",
        "endpoint_id",
        name="deliveries_once",
        postgresql_nulls_not_distinct=True,
    ),
)

delivery_attempts = Table(
    "delivery_attempts",
    metadata,
    Column(
        "delivery_id",
        Uuid,
        ForeignKey("deliveries.id", ondelete="CASCADE"),
        primary_key=True,
    ),
    Column("seq", Integer, primary_key=True),
    Column("attempt", Integer, nullable=False),
    Column("started_at", TS, nullable=False, server_default=text("now()")),
    Column("finished_at", TS),
    Column("outcome", Text),
    Column("error", Text),
)

dead_letters = Table(
    "dead_letters",
    metadata,
    Column("id", BigInteger, Identity(always=True), primary_key=True),
    Column("job_id", BigInteger, ForeignKey("jobs.id", ondelete="CASCADE"), nullable=False),
    Column("queue", Text, nullable=False),
    Column("payload", JSONB, nullable=False),
    Column("error", Text, nullable=False),
    Column("created_at", TS, nullable=False, server_default=text("now()")),
    Column("replayed_at", TS),
    Index("dead_letters_job", "job_id"),
)

article_feedback = Table(
    "article_feedback",
    metadata,
    Column("user_id", Uuid, ForeignKey("users.id"), primary_key=True),
    Column("article_id", BigInteger, ForeignKey("articles.id"), primary_key=True),
    Column("verdict", Text, nullable=False),
    Column("note", Text, nullable=False, server_default=""),
    Column("context", Text, nullable=False),
    Column("created_at", TS, nullable=False, server_default=text("now()")),
    Column("updated_at", TS, nullable=False, server_default=text("now()")),
    Index("article_feedback_article", "article_id"),
)

jobs = Table(
    "jobs",
    metadata,
    Column("id", BigInteger, Identity(always=True), primary_key=True),
    Column("queue", Text, nullable=False),
    Column("kind", Text, nullable=False),
    Column("payload", JSONB, nullable=False, server_default="{}"),
    Column("dedupe_key", Text),
    Column("group_key", Text),
    Column("state", Text, nullable=False, server_default="queued"),
    Column("priority", SmallInteger, nullable=False, server_default="0"),
    Column("run_at", TS, nullable=False, server_default=text("now()")),
    Column("attempts", Integer, nullable=False, server_default="0"),
    Column("max_attempts", Integer, nullable=False, server_default="5"),
    Column("deferrals", Integer, nullable=False, server_default="0"),
    Column("locked_by", Text),
    Column("lease_expires_at", TS),
    Column("last_error", Text),
    Column("created_at", TS, nullable=False, server_default=text("now()")),
    Column("finished_at", TS),
    Index(
        "jobs_dedupe_live",
        "dedupe_key",
        unique=True,
        postgresql_where=text("state IN ('queued', 'running')"),
    ),
    Index(
        "jobs_claim",
        "queue",
        text("priority DESC"),
        "run_at",
        postgresql_where=text("state = 'queued'"),
    ),
    Index("jobs_group", "queue", "group_key", "run_at", postgresql_where=text("state = 'queued'")),
    Index("jobs_lease", "lease_expires_at", postgresql_where=text("state = 'running'")),
)
