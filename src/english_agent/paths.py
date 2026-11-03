from pathlib import Path

_THIS_FILE = Path(__file__).resolve()

# CEFR level -> curriculum file. Declaration order is the learning order.
# Annotated as str (not Cefr) to keep this module free of project imports.
_CURRICULUM_FILES: dict[str, str] = {
    "A2": "a2_topics.yaml",
    "B1": "b1_topics.yaml",
    "B2": "b2_topics.yaml",
    "C1": "c1_topics.yaml",
}


def repo_root() -> Path:
    for parent in _THIS_FILE.parents:
        if (parent / "curriculum" / "a2_topics.yaml").exists():
            return parent
    raise FileNotFoundError("Could not find repository root (missing curriculum/a2_topics.yaml)")


def curriculum_path(cefr: str = "A2") -> Path:
    try:
        name = _CURRICULUM_FILES[cefr]
    except KeyError:
        raise ValueError(f"unknown CEFR level {cefr!r}; known: {sorted(_CURRICULUM_FILES)}") from None
    return repo_root() / "curriculum" / name


def available_cefr_levels() -> list[str]:
    """Levels that actually have a curriculum file on disk, in learning order."""
    root = repo_root() / "curriculum"
    return [cefr for cefr, name in _CURRICULUM_FILES.items() if (root / name).exists()]


def goldens_v1_yaml_path() -> Path:
    return repo_root() / "evals" / "datasets" / "goldens-v1.yaml"


def goldens_v1_jsonl_path() -> Path:
    return repo_root() / "evals" / "datasets" / "goldens-v1.jsonl"
