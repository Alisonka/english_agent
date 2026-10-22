from __future__ import annotations

import json
from pathlib import Path

import yaml
from pydantic import BaseModel, Field, field_validator

from english_agent.models.lesson import EXAMPLE_MAX, EXAMPLE_MIN
from english_agent.paths import goldens_v1_jsonl_path, goldens_v1_yaml_path


class TypicalL1RuError(BaseModel):
    incorrect: str = Field(min_length=1, max_length=240)
    correct: str = Field(min_length=1, max_length=240)
    why_ru: str = Field(min_length=10, max_length=400)


class ExampleConstraints(BaseModel):
    min_examples: int = EXAMPLE_MIN
    max_examples: int = EXAMPLE_MAX
    must_include_construction: str = Field(min_length=1)
    avoid_structures: list[str] = Field(min_length=1)
    notes: str | None = None

    @field_validator("min_examples")
    @classmethod
    def min_matches_schema(cls, value: int) -> int:
        if value != EXAMPLE_MIN:
            raise ValueError(f"min_examples must be {EXAMPLE_MIN}")
        return value

    @field_validator("max_examples")
    @classmethod
    def max_matches_schema(cls, value: int) -> int:
        if value != EXAMPLE_MAX:
            raise ValueError(f"max_examples must be {EXAMPLE_MAX}")
        return value


class GoldenCase(BaseModel):
    """Spec of a good A2 lesson. Used by later evals; not shown to the learner."""

    id: str = Field(pattern=r"^g\d{2}$")
    topic_id: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    expected_key_points: list[str] = Field(min_length=3, max_length=8)
    forbidden_claims: list[str] = Field(min_length=2, max_length=8)
    must_show_form: str = Field(min_length=1, max_length=160)
    typical_l1_ru_error: TypicalL1RuError
    example_constraints: ExampleConstraints


def load_goldens_yaml(path: Path | None = None) -> list[GoldenCase]:
    return load_goldens(path or goldens_v1_yaml_path())


def dump_goldens_jsonl(goldens: list[GoldenCase], path: Path | None = None) -> Path:
    target = path or goldens_v1_jsonl_path()
    lines = [
        case.model_dump_json(ensure_ascii=False)
        for case in goldens
    ]
    target.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return target


def load_goldens(path: Path | None = None) -> list[GoldenCase]:
    target = path or goldens_v1_jsonl_path()
    text = target.read_text(encoding="utf-8")
    if target.suffix in {".yaml", ".yml"}:
        payload = yaml.safe_load(text)
        if not isinstance(payload, list):
            raise ValueError(f"{target} must be a YAML list")
        records = payload
    else:
        records = [json.loads(line) for line in text.splitlines() if line.strip()]
    goldens = [GoldenCase.model_validate(record) for record in records]
    ids = [case.id for case in goldens]
    topic_ids = [case.topic_id for case in goldens]
    if len(ids) != len(set(ids)):
        raise ValueError("golden ids must be unique")
    if len(topic_ids) != len(set(topic_ids)):
        raise ValueError("golden topic_ids must be unique")
    return goldens
