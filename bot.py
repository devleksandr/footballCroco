import os
import random
import sqlite3
import logging
from pathlib import Path

from dotenv import load_dotenv
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    filters,
)

load_dotenv()

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Words
# ---------------------------------------------------------------------------
WORDS_FILE = Path(__file__).parent / "words.txt"
WORDS: list[str] = []


def load_words() -> None:
    global WORDS
    with open(WORDS_FILE, encoding="utf-8") as f:
        WORDS = [line.strip() for line in f if line.strip()]
    if not WORDS:
        raise RuntimeError("words.txt is empty")


def random_word() -> str:
    return random.choice(WORDS)


# ---------------------------------------------------------------------------
# Database (ratings)
# ---------------------------------------------------------------------------
DB_PATH = Path(__file__).parent / "rating.db"


def _db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS ratings (
            chat_id  INTEGER NOT NULL,
            user_id  INTEGER NOT NULL,
            username TEXT    NOT NULL DEFAULT '',
            score    INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (chat_id, user_id)
        )
        """
    )
    conn.commit()
    return conn


def increment_score(chat_id: int, user_id: int, username: str) -> None:
    conn = _db()
    conn.execute(
        """
        INSERT INTO ratings (chat_id, user_id, username, score)
        VALUES (?, ?, ?, 1)
        ON CONFLICT(chat_id, user_id)
        DO UPDATE SET score = score + 1, username = excluded.username
        """,
        (chat_id, user_id, username),
    )
    conn.commit()
    conn.close()


def get_top(chat_id: int, limit: int = 10) -> list[tuple[str, int]]:
    conn = _db()
    rows = conn.execute(
        "SELECT username, score FROM ratings WHERE chat_id = ? ORDER BY score DESC LIMIT ?",
        (chat_id, limit),
    ).fetchall()
    conn.close()
    return rows


# ---------------------------------------------------------------------------
# Game state (per-chat, in-memory)
# ---------------------------------------------------------------------------
games: dict[int, dict] = {}


def _keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("👀 Глянути слово", callback_data="show_word"),
                InlineKeyboardButton("⏭ Наступне слово", callback_data="next_word"),
            ]
        ]
    )


# ---------------------------------------------------------------------------
# /start — begin the game
# ---------------------------------------------------------------------------
async def cmd_start(update: Update, _) -> None:
    chat_id = update.effective_chat.id
    user = update.effective_user

    if chat_id in games and games[chat_id]["active"]:
        await update.message.reply_text("Гра вже йде! Вгадуйте слово 🐊")
        return

    word = random_word()
    games[chat_id] = {
        "word": word,
        "leader_id": user.id,
        "leader_name": user.first_name,
        "active": True,
    }

    await update.message.reply_text(
        f"🐊 Гру розпочато!\n\n"
        f"Загадує: <b>{user.first_name}</b>\n"
        f"Натисніть кнопку нижче, щоб побачити слово (тільки загадуючий).",
        parse_mode="HTML",
        reply_markup=_keyboard(),
    )


# ---------------------------------------------------------------------------
# /stop — end the game
# ---------------------------------------------------------------------------
async def cmd_stop(update: Update, _) -> None:
    chat_id = update.effective_chat.id
    if chat_id in games and games[chat_id]["active"]:
        games[chat_id]["active"] = False
        await update.message.reply_text("🛑 Гру зупинено.")
    else:
        await update.message.reply_text("Зараз жодна гра не йде.")


# ---------------------------------------------------------------------------
# /rating — show leaderboard
# ---------------------------------------------------------------------------
async def cmd_rating(update: Update, _) -> None:
    chat_id = update.effective_chat.id
    top = get_top(chat_id)
    if not top:
        await update.message.reply_text("Рейтинг порожній — зіграйте хоча б раз!")
        return

    lines = ["🏆 <b>Рейтинг гравців:</b>\n"]
    medals = {1: "🥇", 2: "🥈", 3: "🥉"}
    for i, (username, score) in enumerate(top, start=1):
        prefix = medals.get(i, f"{i}.")
        lines.append(f"{prefix} {username} — {score} слів")

    await update.message.reply_text("\n".join(lines), parse_mode="HTML")


# ---------------------------------------------------------------------------
# Inline-button callbacks
# ---------------------------------------------------------------------------
async def button_handler(update: Update, _) -> None:
    query = update.callback_query
    chat_id = query.message.chat_id
    user_id = query.from_user.id

    game = games.get(chat_id)
    if not game or not game["active"]:
        await query.answer("Гра не активна.", show_alert=True)
        return

    if query.data == "show_word":
        if user_id != game["leader_id"]:
            await query.answer("Тільки загадуючий може дивитись слово!", show_alert=True)
            return
        await query.answer(f"Слово: {game['word']}", show_alert=True)

    elif query.data == "next_word":
        if user_id != game["leader_id"]:
            await query.answer("Тільки загадуючий може змінити слово!", show_alert=True)
            return
        game["word"] = random_word().upper()
        await query.answer(f"Нове слово: {game['word']}", show_alert=True)


# ---------------------------------------------------------------------------
# Message handler — guess checking
# ---------------------------------------------------------------------------
async def guess_handler(update: Update, _) -> None:
    if not update.message or not update.message.text:
        return

    chat_id = update.effective_chat.id
    game = games.get(chat_id)
    if not game or not game["active"]:
        return

    user = update.effective_user
    if user.id == game["leader_id"]:
        return

    guess = update.message.text.strip().lower()
    if guess == game["word"].lower():
        display_name = user.first_name
        increment_score(chat_id, user.id, display_name)

        new_word = random_word()
        game["word"] = new_word
        game["leader_id"] = user.id
        game["leader_name"] = display_name

        await update.message.reply_text(
            f"🎉 <b>{display_name}</b> вгадав(ла) слово!\n\n"
            f"Тепер загадує: <b>{display_name}</b>\n"
            f"Натисніть кнопку, щоб побачити нове слово.",
            parse_mode="HTML",
            reply_markup=_keyboard(),
        )


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main() -> None:
    load_words()

    token = os.getenv("BOT_TOKEN")
    if not token:
        raise RuntimeError("BOT_TOKEN not set. Create .env file with BOT_TOKEN=<your token>")

    app = Application.builder().token(token).build()

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("stop", cmd_stop))
    app.add_handler(CommandHandler("rating", cmd_rating))
    app.add_handler(CallbackQueryHandler(button_handler))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, guess_handler))

    logger.info("Bot started")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
