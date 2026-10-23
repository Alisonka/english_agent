from __future__ import annotations

import re

from english_agent import __version__
from english_agent.versions import (
    PROMPT_FILES,
    SCHEMA_VERSIONS,
    Provenance,
    build_provenance,
    fingerprint_text,
    prompt_fingerprint,
)


def test_fingerprint_is_stable_and_short() -> None:
    first = fingerprint_text("hello")
    assert first == fingerprint_text("hello")
    assert fingerprint_text("hello") != fingerprint_text("world")
    assert re.fullmatch(r"[0-9a-f]{12}", first)


def test_prompt_fingerprints_cover_known_prompts() -> None:
    for name in PROMPT_FILES:
        assert re.fullmatch(r"[0-9a-f]{12}", prompt_fingerprint(name))


def test_build_provenance_collects_component_versions() -> None:
    prov = build_provenance(
        component="writer",
        schema_version=SCHEMA_VERSIONS["lesson"],
        model="claude-haiku-5-5",
        prompt_names=("write_lesson_system",),
        curriculum_version=3,
    )
    assert isinstance(prov, Provenance)
    assert prov.pipeline_version == __version__
    assert prov.component == "writer"
    assert prov.schema_version == SCHEMA_VERSIONS["lesson"]
    assert prov.model == "claude-haiku-5-5"
    assert prov.prompts["write_lesson_system"] == prompt_fingerprint("write_lesson_system")
    assert prov.curriculum_version == 3
    assert prov.generated_at.tzinfo is not None
