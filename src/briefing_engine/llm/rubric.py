"""A profile's rubric and its hash (§7.3, ADR-005).

The rubric hash keys the score cache, so two rubrics that are equal as data must hash
the same however their keys were ordered. It is SHA-256 of the canonical JSON.
"""

from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from briefing_engine.hashing import canonical_json, sha256


class Interest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    topic: str = Field(min_length=1)
    weight: int = Field(ge=1, le=5)


class Rubric(BaseModel):
    model_config = ConfigDict(extra="forbid")

    description: str = Field(min_length=1)
    interests: list[Interest] = Field(min_length=1)
    avoid: list[str] = Field(default_factory=list)
    scale_hint: str = Field(min_length=1)

    def as_json(self) -> dict[str, Any]:
        return self.model_dump(mode="json")


def rubric_hash(rubric: Rubric) -> bytes:
    return sha256(canonical_json(rubric.as_json()))
