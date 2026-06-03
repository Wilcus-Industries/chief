"""S0b — prove a Telegram message round-trips through the Agent SDK.

Throwaway de-risking slice (DESIGN.md S0). Long-polls Telegram; for a DM from the
configured owner it routes the text into the same one-shot SDK query as
``verify_auth`` and replies with the model output.

Deliberately minimal: no tiers, topics, gate, or persistence — those land at M0+.
Only the owner is answered; everything else is ignored.
"""

import logging
import os

from claude_agent_sdk import AssistantMessage, ClaudeAgentOptions, TextBlock, query
from telegram import Update
from telegram.ext import Application, ContextTypes, MessageHandler, filters

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
logger = logging.getLogger("s0.bot")


async def ask_claude(prompt: str) -> str:
    """Run one stateless SDK turn and return the concatenated assistant text."""
    options = ClaudeAgentOptions(max_turns=1)
    parts: list[str] = []
    async for message in query(prompt=prompt, options=options):
        if isinstance(message, AssistantMessage):
            parts += [b.text for b in message.content if isinstance(b, TextBlock)]
    return "".join(parts).strip() or "(no reply)"


async def on_owner_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Answer a text DM only when it comes from the configured owner."""
    message = update.effective_message
    user = update.effective_user
    if message is None or user is None or not message.text:
        return
    if user.id != context.bot_data["owner_id"]:
        logger.info("ignoring message from non-owner %s", user.id)
        return

    logger.info("owner message: %s", message.text)
    reply = await ask_claude(message.text)
    await message.reply_text(reply)


def main() -> None:
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    owner_id = os.environ.get("OWNER_TELEGRAM_ID")
    if not token:
        raise SystemExit("TELEGRAM_BOT_TOKEN is not set.")
    if not owner_id:
        raise SystemExit("OWNER_TELEGRAM_ID is not set.")
    if os.environ.get("ANTHROPIC_API_KEY"):
        raise SystemExit(
            "ANTHROPIC_API_KEY is set — it would bill the API instead of the Max "
            "subscription. Unset it before running S0."
        )

    app = Application.builder().token(token).build()
    app.bot_data["owner_id"] = int(owner_id)
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_owner_message))
    logger.info("S0b bot starting (long-poll); answering owner %s only", owner_id)
    app.run_polling()


if __name__ == "__main__":
    main()
