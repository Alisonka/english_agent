"""Outgoing Telegram messages through aiogram: what the lesson, practice and bot code sends.

`Sender` hides aiogram's method signatures behind the few calls the project makes and keeps
the old client's guarantees: HTML parse mode and no link previews (bot defaults), plain-text
quiz explanations, and lessons split into ordered messages. Errors are aiogram's:
`TelegramError` for any API failure, `TelegramForbiddenError` when the user blocked the bot.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable

from aiogram import Bot
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.client.session.base import BaseSession
from aiogram.client.telegram import TelegramAPIServer
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramAPIError as TelegramError
from aiogram.exceptions import TelegramForbiddenError
from aiogram.types import ForceReply, InlineKeyboardMarkup, InputPollOption

from english_agent.config import Settings
from english_agent.models.lesson import Lesson
from english_agent.telegram.render import render_messages
from english_agent.telegram.throttle import LocalRateLimiter, RateLimiter, ThrottlingMiddleware
from english_agent.tracing import get_tracer

__all__ = ["Sender", "TelegramError", "TelegramForbiddenError", "make_bot"]

ReplyMarkup = InlineKeyboardMarkup | ForceReply


def make_bot(settings: Settings, *, session: BaseSession | None = None, limiter: RateLimiter | None = None) -> Bot:
    """The process's bot: HTML by default, no link previews, every request rate-limited and 429-safe."""
    token = settings.telegram_bot_token.get_secret_value()
    if not token:
        raise ValueError("TELEGRAM_BOT_TOKEN is not set")
    if session is None:
        session = AiohttpSession(api=TelegramAPIServer.from_base(settings.telegram_api_base))
    session.middleware(
        ThrottlingMiddleware(
            limiter
            or LocalRateLimiter(
                global_per_second=settings.telegram_global_rate_per_second,
                chat_per_second=settings.telegram_chat_rate_per_second,
            )
        )
    )
    defaults = DefaultBotProperties(parse_mode=ParseMode.HTML, link_preview_is_disabled=True)
    return Bot(token, session=session, default=defaults)


class Sender:
    def __init__(self, bot: Bot) -> None:
        self.bot = bot

    async def send_text(self, chat_id: str, text: str, *, reply_markup: ReplyMarkup | None = None) -> int:
        """Send one HTML message; returns its message_id."""
        message = await self.bot.send_message(chat_id, text, reply_markup=reply_markup)
        return message.message_id

    async def edit_text(
        self, chat_id: str, message_id: int, text: str, *, reply_markup: InlineKeyboardMarkup | None = None
    ) -> None:
        await self.bot.edit_message_text(text=text, chat_id=chat_id, message_id=message_id, reply_markup=reply_markup)

    async def send_poll(
        self, chat_id: str, question: str, options: list[str], *, correct_option: int, explanation: str
    ) -> tuple[int, str]:
        """A quiz poll, not anonymous so answers arrive as `poll_answer`; returns (message_id, poll_id)."""
        message = await self.bot.send_poll(
            chat_id,
            question,
            [InputPollOption(text=option) for option in options],
            type="quiz",
            is_anonymous=False,
            correct_option_ids=[correct_option],
            explanation=explanation,
            explanation_parse_mode=None,  # the explanation is plain text, not HTML
        )
        assert message.poll is not None
        return message.message_id, message.poll.id

    async def send_lesson(
        self,
        chat_id: str,
        lesson: Lesson,
        *,
        skip: int = 0,
        on_sent: Callable[[int], Awaitable[None]] | None = None,
    ) -> list[int]:
        """Send the lesson's messages in order, after the first `skip` (sent before a crash).

        `on_sent` is awaited with each message id as soon as Telegram accepts it, so a caller
        that records progress can resume without sending a chunk twice.
        """
        with get_tracer().start_as_current_span("telegram.send_lesson") as span:
            chunks = render_messages(lesson)
            span.set_attribute("telegram.messages", len(chunks))
            span.set_attribute("telegram.skipped", skip)
            sent: list[int] = []
            for chunk in chunks[skip:]:
                message_id = await self.send_text(chat_id, chunk)
                sent.append(message_id)
                if on_sent is not None:
                    await on_sent(message_id)
            return sent
