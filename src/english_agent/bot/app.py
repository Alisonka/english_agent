"""Building the dispatcher and running the bot by long polling (webhook mode in a later step)."""

from __future__ import annotations

import logging

from aiogram import Bot, Dispatcher
from aiogram.types import BotCommand

from english_agent.bot.deps import BotDeps
from english_agent.bot.handlers import COMMANDS, build_router
from english_agent.telegram.sender import TelegramError

log = logging.getLogger(__name__)

ALLOWED_UPDATES = ["message", "callback_query", "poll_answer"]


def build_dispatcher(deps: BotDeps) -> Dispatcher:
    """A dispatcher whose handlers get `deps` injected by name."""
    dispatcher = Dispatcher(deps=deps)
    dispatcher.include_router(build_router())
    dispatcher.startup.register(_register_commands)
    return dispatcher


async def _register_commands(bot: Bot) -> None:
    """Make the commands show in the bot's `/` menu; a failure is not a reason to stay down."""
    try:
        await bot.set_my_commands([BotCommand(command=name, description=text) for name, text in COMMANDS])
    except TelegramError as error:
        log.warning("could not register commands: %s", error)
