"""Storage maintenance commands.

uv run python -m english_agent.storage import-learner-json                    # .state/learner.json -> learners
uv run python -m english_agent.storage import-learner-json - < learner.json   # read the file from stdin
uv run python -m english_agent.storage backfill-items                         # core items for already sent lessons
uv run python -m english_agent.storage backfill-reviews                       # FSRS ratings for already stored answers
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from pydantic import BaseModel, ValidationError
from sqlalchemy.exc import SQLAlchemyError

from english_agent.config import get_settings
from english_agent.items import backfill_core_items
from english_agent.logs import configure_logging
from english_agent.models.cefr import Cefr
from english_agent.srs import make_scheduler
from english_agent.storage.db import make_engine, make_session_factory
from english_agent.storage.repositories import MaintenanceRepo, open_stores

log = logging.getLogger(__name__)


class _LegacyLearnerState(BaseModel):
    """Shape of the `.state/learner.json` file the bot wrote before the database existed."""

    cefr: Cefr


def _import_learner_json(path: str) -> int:
    settings = get_settings()
    if not settings.telegram_chat_id:
        log.error("TELEGRAM_CHAT_ID must be set: the learner is stored under that chat")
        return 2
    stores = open_stores(settings)
    if stores is None:
        log.error("DATABASE_URL must be set")
        return 2

    try:
        text = sys.stdin.read() if path == "-" else Path(path).read_text(encoding="utf-8")
        state = _LegacyLearnerState.model_validate_json(text)
    except (OSError, ValidationError) as error:
        log.error("cannot read learner state from %s: %s", path, error)
        return 1

    record = stores.learners.set_cefr(settings.telegram_chat_id, state.cefr)
    print(f"learner {record.id} (chat {record.telegram_chat_id}): cefr={record.cefr}")
    return 0


def _backfill_items() -> int:
    stores = open_stores(get_settings())
    if stores is None:
        log.error("DATABASE_URL must be set")
        return 2
    try:
        result = backfill_core_items(stores)
    except SQLAlchemyError as error:
        log.error("backfill failed: %s", error)
        return 1
    print(f"items: {result.created} created, {result.existing} already present, {result.skipped} lessons skipped")
    return 0


def _backfill_reviews() -> int:
    """Run after `backfill-items`: links old polls to items, then rates every stored answer once."""
    settings = get_settings()
    if not settings.database_url.get_secret_value():
        log.error("DATABASE_URL must be set")
        return 2
    maintenance = MaintenanceRepo(make_session_factory(make_engine(settings)), make_scheduler(settings))
    try:
        linked = maintenance.link_polls_to_items()
        applied = maintenance.replay_reviews()
    except SQLAlchemyError as error:
        log.error("backfill failed: %s", error)
        return 1
    print(f"reviews: {linked} polls linked to items, {applied} ratings applied")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Storage maintenance commands.")
    commands = parser.add_subparsers(dest="command", required=True)
    learner_json = commands.add_parser(
        "import-learner-json", help="copy the level from the old .state/learner.json into the learners table"
    )
    learner_json.add_argument("path", nargs="?", default=".state/learner.json", help="state file, or - for stdin")
    commands.add_parser("backfill-items", help="create the core item of every sent lesson that has none")
    commands.add_parser("backfill-reviews", help="rate stored quiz and translation answers that have no review yet")
    args = parser.parse_args(argv)
    configure_logging(get_settings())

    if args.command == "import-learner-json":
        return _import_learner_json(args.path)
    if args.command == "backfill-items":
        return _backfill_items()
    if args.command == "backfill-reviews":
        return _backfill_reviews()
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
