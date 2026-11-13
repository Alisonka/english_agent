"""Load test: N learners due at the same minute, through the real scheduler, worker and bot code.

Run against a stack of its own (never production: it creates learners), whose services talk to
`scripts/fake_telegram.py` instead of Telegram:

    export POSTGRES_PORT=15432 REDIS_PORT=16379 JAEGER_UI_PORT=26686 JAEGER_GRPC_PORT=24317 JAEGER_OTLP_PORT=24318
    docker compose -p english-agent-soak -f docker-compose.yml -f docker-compose.soak.yml up -d --build
    docker compose -p english-agent-soak -f docker-compose.yml -f docker-compose.soak.yml \\
        run --rm soak python scripts/soak.py --learners 500

It seeds the learners (lesson time: the next minute, UTC) and a ready shared lesson for their
first topic, so no Claude call and no research site is involved; then it waits until every
lesson is sent and checks, against the fake API's record, that every learner got the lesson
exactly once (no message twice) and reports how often Telegram's limits were hit.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import time
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx

from english_agent.config import get_settings
from english_agent.content import content_key
from english_agent.models.curriculum import load_curriculum
from english_agent.models.lesson import Lesson
from english_agent.models.schedule import LearnerSchedule
from english_agent.storage.repositories import Stores, open_stores
from english_agent.telegram.render import render_messages

CHAT_BASE = 9_000_000_000  # far from real Telegram user ids


def sample_lesson(topic_id: str) -> Lesson:
    """A valid lesson for the soak's topic (the content is irrelevant; only its delivery is tested)."""
    question = {
        "kind": "gap_fill",
        "question": "She ___ a teacher.",
        "options": ["am", "is", "are"],
        "correct_option": 1,
        "explanation_ru": "She — третье лицо единственного числа, поэтому is.",
    }
    task = {"ru": "Я студент.", "reference_en": "I am a student.", "focus_ru": "I + am"}
    return Lesson.model_validate(
        {
            "topic_id": topic_id,
            "title_en": "am / is / are",
            "title_ru": "be в настоящем — am, is, are",
            "cefr": "A2",
            "key_idea_ru": "Где по-русски «я — студент» без глагола, по-английски нужен глагол be: am, is или are.",
            "rule_ru": (
                "Глагол be в настоящем времени меняет форму по лицу: I am, he/she/it is, we/you/they are. "
                "По-русски связку обычно опускаем, по-английски она обязательна: She is a doctor. "
                "В отрицании добавляем not: I am not tired. В вопросе глагол выходит вперёд: Are you ready? "
                "В разговоре используем сокращения: I'm, she's, they're."
            ),
            "form": "I am / he, she, it is / we, you, they are",
            "nuances_ru": [
                "С I всегда am: I am late.",
                "С he, she, it и единственным числом — is: The book is new.",
                "С we, you, they и множественным числом — are: They are at home.",
            ],
            "examples": [
                {"en": "I am a student.", "ru": "Я студент.", "highlights": ["am"]},
                {"en": "She is at work.", "ru": "Она на работе.", "highlights": ["is"]},
                {"en": "They are friends.", "ru": "Они друзья.", "highlights": ["are"]},
                {"en": "Are you ready?", "ru": "Ты готов?", "highlights": ["Are"]},
            ],
            "mistake_ru": "Калька с русского «я студент» даёт I student без глагола.",
            "incorrect": "I student.",
            "correct": "I am a student.",
            "sources": [
                {"url": "https://skysmart.ru/articles/english/to-be", "title": "to be", "accessed_at": "2026-11-01"}
            ],
            "quiz": [question, question | {"question": "They ___ at home."}, question | {"question": "I ___ late."}],
            "translation": [task, task | {"ru": "Она дома."}, task | {"ru": "Мы готовы."}],
        }
    )


async def seed(stores: Stores, learners: int, send_at: datetime) -> tuple[list[int], int]:
    """The learners and the ready shared lesson; returns the learner ids and messages per lesson."""
    settings = get_settings()
    curriculum = load_curriculum("A2")
    topic = sorted(curriculum.topics, key=lambda t: t.order)[0]
    lesson = sample_lesson(topic.id)
    outcome, record = await stores.contents.claim(
        topic.id,
        "A2",
        curriculum.version,
        content_key(settings),
        owner="soak",
        now=datetime.now(UTC),
        stale_after=timedelta(minutes=1),
        max_attempts=1000,
    )
    if outcome != "ready":
        await stores.contents.finish(record.id, lesson)
    schedule = LearnerSchedule(timezone="UTC", lesson_send_time=send_at.time().replace(second=0, microsecond=0))
    ids = []
    for n in range(learners):
        chat_id = str(CHAT_BASE + n)
        learner = await stores.learners.get_or_create(chat_id, "A2", schedule)
        await stores.learners.set_schedule(chat_id, schedule)
        await stores.learners.start_course(chat_id, send_at.date())
        ids.append(learner.id)
    return ids, len(render_messages(lesson))


async def wait_until_sent(stores: Stores, ids: list[int], day: Any, timeout: float) -> tuple[int, float]:
    started = time.monotonic()
    while True:
        states = await stores.lessons.states([(learner_id, day) for learner_id in ids])
        sent = sum(state.status == "sent" for state in states.values())
        elapsed = time.monotonic() - started
        print(f"{elapsed:6.0f}s  sent {sent}/{len(ids)}", flush=True)
        if sent == len(ids) or elapsed > timeout:
            return sent, elapsed
        await asyncio.sleep(10)


async def run(args: argparse.Namespace) -> int:
    settings = get_settings()
    async with open_stores(settings) as stores:
        if stores is None:
            print("DATABASE_URL must be set", file=sys.stderr)
            return 2
        send_at = (datetime.now(UTC) + timedelta(minutes=1)).replace(second=0, microsecond=0)
        ids, per_lesson = await seed(stores, args.learners, send_at)
        print(f"seeded {len(ids)} learners due at {send_at:%H:%M} UTC; {per_lesson} message(s) per lesson")
        sent, elapsed = await wait_until_sent(stores, ids, send_at.date(), args.timeout)

    stats = httpx.get(f"{args.fake.rstrip('/')}/stats", timeout=10).json()
    received = stats["messages"]
    ours = {str(CHAT_BASE + n) for n in range(args.learners)}
    short = sorted(chat for chat in ours if received.get(chat, 0) < per_lesson)
    extra = sorted(chat for chat in ours if received.get(chat, 0) > per_lesson)
    print(f"lessons sent: {sent}/{len(ids)} in {elapsed:.0f}s")
    print(f"chats short of the lesson: {len(short)}; with more than the lesson: {len(extra)}")
    print(f"duplicate messages: {sum(stats['duplicates'].get(chat, 0) for chat in ours)}")
    print(f"429s answered by the fake API: {stats['flood_waits']}")
    ok = sent == len(ids) and not short and not extra and not any(stats["duplicates"].get(c) for c in ours)
    print("OK" if ok else "FAILED")
    return 0 if ok else 1


def main() -> int:
    parser = argparse.ArgumentParser(description="Load test of the lesson delivery path.")
    parser.add_argument("--learners", type=int, default=200)
    parser.add_argument("--fake", default="http://fake-telegram:8081", help="the fake Bot API (for /stats)")
    parser.add_argument("--timeout", type=float, default=900, help="seconds to wait for every lesson")
    return asyncio.run(run(parser.parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
