from pathlib import Path

_THIS_FILE = Path(__file__).resolve()


def repo_root() -> Path:
    for parent in _THIS_FILE.parents:
        if (parent / "curriculum" / "a2_topics.yaml").exists():
            return parent
    raise FileNotFoundError("Could not find repository root (missing curriculum/a2_topics.yaml)")


def curriculum_path() -> Path:
    return repo_root() / "curriculum" / "a2_topics.yaml"


def goldens_v1_yaml_path() -> Path:
    return repo_root() / "evals" / "datasets" / "goldens-v1.yaml"


def goldens_v1_jsonl_path() -> Path:
    return repo_root() / "evals" / "datasets" / "goldens-v1.jsonl"
