"""Storage maintenance commands.

uv run python -m english_agent.storage import-learner-json                    # .state/learner.json -> learners
uv run python -m english_agent.storage import-learner-json - < learner.json   # read the file from stdin
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from pydantic import BaseModel, ValidationError

from english_agent.config import get_settings
from english_agent.models.cefr import Cefr
from english_agent.storage.repositories import open_stores


class _LegacyLearnerState(BaseModel):
    """Shape of the `.state/learner.json` file the bot wrote before the database existed."""

    cefr: Cefr


def _import_learner_json(path: str) -> int:
    settings = get_settings()
    if not settings.telegram_chat_id:
        print("TELEGRAM_CHAT_ID must be set: the learner is stored under that chat", file=sys.stderr)
        return 2
    stores = open_stores(settings)
    if stores is None:
        print("DATABASE_URL must be set", file=sys.stderr)
        return 2

    try:
        text = sys.stdin.read() if path == "-" else Path(path).read_text(encoding="utf-8")
        state = _LegacyLearnerState.model_validate_json(text)
    except (OSError, ValidationError) as error:
        print(f"cannot read learner state from {path}: {error}", file=sys.stderr)
        return 1

    record = stores.learners.set_cefr(settings.telegram_chat_id, state.cefr)
    print(f"learner {record.id} (chat {record.telegram_chat_id}): cefr={record.cefr}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Storage maintenance commands.")
    commands = parser.add_subparsers(dest="command", required=True)
    learner_json = commands.add_parser(
        "import-learner-json", help="copy the level from the old .state/learner.json into the learners table"
    )
    learner_json.add_argument("path", nargs="?", default=".state/learner.json", help="state file, or - for stdin")
    args = parser.parse_args(argv)

    if args.command == "import-learner-json":
        return _import_learner_json(args.path)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
