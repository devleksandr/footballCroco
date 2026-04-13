import os
import time
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
    ContextTypes,
    filters,
)

load_dotenv()

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
CLAIM_PRIORITY_SECONDS = 10
ROUND_TIMEOUT_SECONDS = 600  # 10 minutes

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


def pick_word(used: set[str]) -> str:
    """Pick a random word that hasn't been guessed in this game yet.

    `used` contains lowercased words. If every word has already been
    guessed, the pool resets and we just pick anything.
    """
    available = [w for w in WORDS if w.lower() not in used]
    if not available:
        available = WORDS
    return random.choice(available)


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
            likes    INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (chat_id, user_id)
        )
        """
    )
    # migration: add likes column if missing
    cols = {row[1] for row in conn.execute("PRAGMA table_info(ratings)").fetchall()}
    if "likes" not in cols:
        conn.execute("ALTER TABLE ratings ADD COLUMN likes INTEGER NOT NULL DEFAULT 0")
    conn.commit()
    return conn


def increment_score(chat_id: int, user_id: int, username: str) -> None:
    conn = _db()
    conn.execute(
        """
        INSERT INTO ratings (chat_id, user_id, username, score, likes)
        VALUES (?, ?, ?, 1, 0)
        ON CONFLICT(chat_id, user_id)
        DO UPDATE SET score = score + 1, username = excluded.username
        """,
        (chat_id, user_id, username),
    )
    conn.commit()
    conn.close()


def increment_likes(chat_id: int, user_id: int, username: str) -> None:
    conn = _db()
    conn.execute(
        """
        INSERT INTO ratings (chat_id, user_id, username, score, likes)
        VALUES (?, ?, ?, 0, 1)
        ON CONFLICT(chat_id, user_id)
        DO UPDATE SET likes = likes + 1, username = excluded.username
        """,
        (chat_id, user_id, username),
    )
    conn.commit()
    conn.close()


def get_top(chat_id: int, limit: int = 10) -> list[tuple[str, int, int]]:
    conn = _db()
    rows = conn.execute(
        "SELECT username, score, likes FROM ratings WHERE chat_id = ? "
        "ORDER BY score DESC, likes DESC LIMIT ?",
        (chat_id, limit),
    ).fetchall()
    conn.close()
    return rows


# ---------------------------------------------------------------------------
# Game state (per-chat, in-memory)
# ---------------------------------------------------------------------------
games: dict[int, dict] = {}


def _leader_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("👀 Глянути слово", callback_data="show_word"),
                InlineKeyboardButton("⏭ Наступне слово", callback_data="next_word"),
            ]
        ]
    )


def _claim_keyboard(round_id: int, like_count: int = 0) -> InlineKeyboardMarkup:
    like_label = f"👍 Лайк поясненню ({like_count})"
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("🎯 Хочу пояснювати", callback_data=f"claim:{round_id}")],
            [InlineKeyboardButton(like_label, callback_data=f"like:{round_id}")],
        ]
    )


def _like_only_keyboard(round_id: int, like_count: int) -> InlineKeyboardMarkup:
    like_label = f"👍 Лайк поясненню ({like_count})"
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton(like_label, callback_data=f"like:{round_id}")]]
    )


# ---------------------------------------------------------------------------
# Round timeout (10 min without a guess => game ends)
# ---------------------------------------------------------------------------
async def round_timeout_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = context.job.chat_id
    game = games.get(chat_id)
    if not game or not game.get("active"):
        return

    word = game.get("word")
    game["active"] = False
    game["timeout_job"] = None

    text = "⏰ 10 хвилин минуло без вгадування — гра завершена."
    if word:
        text += f"\nСлово було: <b>{word}</b>"
    await context.bot.send_message(chat_id, text, parse_mode="HTML")


def cancel_timeout(game: dict) -> None:
    job = game.get("timeout_job")
    if job:
        try:
            job.schedule_removal()
        except Exception:
            pass
    game["timeout_job"] = None


def schedule_timeout(context: ContextTypes.DEFAULT_TYPE, chat_id: int) -> None:
    game = games.get(chat_id)
    if not game:
        return
    cancel_timeout(game)
    game["timeout_job"] = context.job_queue.run_once(
        round_timeout_job, when=ROUND_TIMEOUT_SECONDS, chat_id=chat_id
    )


# ---------------------------------------------------------------------------
# /start — begin the game
# ---------------------------------------------------------------------------
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    user = update.effective_user

    if chat_id in games and games[chat_id].get("active"):
        await update.message.reply_text("Гра вже йде! Вгадуйте слово 🐊")
        return

    used_words: set[str] = set()
    word = pick_word(used_words).upper()
    games[chat_id] = {
        "word": word,
        "leader_id": user.id,
        "leader_name": user.first_name,
        "active": True,
        "claim_open": False,
        "winner_id": None,
        "winner_name": None,
        "claim_open_at": 0.0,
        "pending_round_id": None,  # round awaiting a claim
        "rounds": {},              # round_id -> {explainer_id, explainer_name, likers, claim_taken}
        "next_round_id": 1,
        "timeout_job": None,
        "used_words": used_words,  # lowercased words already guessed in this game
    }

    schedule_timeout(context, chat_id)

    await update.message.reply_text(
        f"🐊 Гру розпочато!\n\n"
        f"Загадує: <b>{user.first_name}</b>\n"
        f"Натисніть кнопку нижче, щоб побачити слово (тільки загадуючий).",
        parse_mode="HTML",
        reply_markup=_leader_keyboard(),
    )


# ---------------------------------------------------------------------------
# /stop — end the game
# ---------------------------------------------------------------------------
async def cmd_stop(update: Update, _) -> None:
    chat_id = update.effective_chat.id
    game = games.get(chat_id)
    if game and game.get("active"):
        game["active"] = False
        cancel_timeout(game)
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
    for i, (username, score, likes) in enumerate(top, start=1):
        prefix = medals.get(i, f"{i}.")
        lines.append(f"{prefix} {username} — {score} 🐊 / {likes} 👍")

    await update.message.reply_text("\n".join(lines), parse_mode="HTML")


# ---------------------------------------------------------------------------
# Inline-button callbacks
# ---------------------------------------------------------------------------
async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    chat_id = query.message.chat_id
    user_id = query.from_user.id
    user_name = query.from_user.first_name

    game = games.get(chat_id)
    data = query.data or ""

    # Likes must work even when the game is no longer active (old messages).
    # So handle likes BEFORE the "game not active" early-return.
    if data.startswith("like:"):
        if not game:
            await query.answer("Гра не знайдена.", show_alert=True)
            return
        try:
            round_id = int(data.split(":", 1)[1])
        except ValueError:
            await query.answer("Некоректні дані.", show_alert=True)
            return
        round_data = game.get("rounds", {}).get(round_id)
        if not round_data:
            await query.answer("Цей раунд вже недоступний.", show_alert=True)
            return
        if user_id == round_data["explainer_id"]:
            await query.answer("Самому собі лайк ставити не можна 🙈", show_alert=True)
            return
        if user_id in round_data["likers"]:
            await query.answer("Ви вже поставили лайк за це пояснення.", show_alert=True)
            return

        round_data["likers"].add(user_id)
        increment_likes(chat_id, round_data["explainer_id"], round_data["explainer_name"])
        await query.answer(
            f"👍 Дякую! Лайк для {round_data['explainer_name']}.",
            show_alert=True,
        )

        # Rebuild the keyboard for THIS message — keep claim button only if
        # claim is still pending for this round.
        count = len(round_data["likers"])
        if round_data["claim_taken"]:
            new_markup = _like_only_keyboard(round_id, count)
        else:
            new_markup = _claim_keyboard(round_id, count)
        try:
            await query.edit_message_reply_markup(reply_markup=new_markup)
        except Exception:
            pass
        return

    # For all other buttons, require an active game.
    if not game or not game.get("active"):
        await query.answer("Гра не активна.", show_alert=True)
        return

    # ---- Leader-only buttons ----
    if data == "show_word":
        if game.get("claim_open"):
            await query.answer("Спочатку оберіть пояснюючого.", show_alert=True)
            return
        if user_id != game["leader_id"]:
            await query.answer("Тільки загадуючий може дивитись слово!", show_alert=True)
            return
        await query.answer(f"🔤 {game['word']}", show_alert=True)

    elif data == "next_word":
        if game.get("claim_open"):
            await query.answer("Спочатку оберіть пояснюючого.", show_alert=True)
            return
        if user_id != game["leader_id"]:
            await query.answer("Тільки загадуючий може змінити слово!", show_alert=True)
            return
        game["word"] = pick_word(game["used_words"]).upper()
        await query.answer(f"🔤 {game['word']}", show_alert=True)

    # ---- Claim ("Хочу пояснювати") ----
    elif data.startswith("claim:"):
        try:
            round_id = int(data.split(":", 1)[1])
        except ValueError:
            await query.answer("Некоректні дані.", show_alert=True)
            return

        round_data = game.get("rounds", {}).get(round_id)
        if not round_data or round_data["claim_taken"]:
            await query.answer("Це пояснення вже передано.", show_alert=True)
            return
        if not game.get("claim_open") or game.get("pending_round_id") != round_id:
            await query.answer("Зараз неможливо стати пояснюючим.", show_alert=True)
            return

        elapsed = time.time() - game["claim_open_at"]
        if elapsed < CLAIM_PRIORITY_SECONDS and user_id != game["winner_id"]:
            remaining = int(CLAIM_PRIORITY_SECONDS - elapsed) + 1
            await query.answer(
                f"Ще {remaining} с — пріоритет у переможця.",
                show_alert=True,
            )
            return

        # Claim accepted — start new round
        new_word = pick_word(game["used_words"]).upper()
        game["word"] = new_word
        game["leader_id"] = user_id
        game["leader_name"] = user_name
        game["claim_open"] = False
        game["winner_id"] = None
        game["winner_name"] = None
        game["pending_round_id"] = None
        round_data["claim_taken"] = True

        schedule_timeout(context, chat_id)

        await query.answer(f"Тепер ви пояснюєте! 🔤 {new_word}", show_alert=True)

        # Leave the like button on the old message so people can still like.
        try:
            await query.edit_message_reply_markup(
                reply_markup=_like_only_keyboard(round_id, len(round_data["likers"]))
            )
        except Exception:
            pass

        await context.bot.send_message(
            chat_id,
            f"🐊 Тепер пояснює: <b>{user_name}</b>\n"
            f"Натисніть кнопку, щоб побачити слово.",
            parse_mode="HTML",
            reply_markup=_leader_keyboard(),
        )


# ---------------------------------------------------------------------------
# Message handler — guess checking
# ---------------------------------------------------------------------------
async def guess_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or not update.message.text:
        return

    chat_id = update.effective_chat.id
    game = games.get(chat_id)
    if not game or not game.get("active"):
        return
    if game.get("claim_open"):
        return  # waiting for someone to claim — guesses ignored

    user = update.effective_user
    if user.id == game["leader_id"]:
        return

    guess = update.message.text.strip().lower()
    if guess != game["word"].lower():
        return

    # ---- Correct guess ----
    word = game["word"]
    display_name = user.first_name
    increment_score(chat_id, user.id, display_name)
    game["used_words"].add(word.lower())

    cancel_timeout(game)

    # Register a new round entry so that likes for this explanation are
    # tracked separately and survive claim transitions.
    round_id = game["next_round_id"]
    game["next_round_id"] = round_id + 1
    game["rounds"][round_id] = {
        "explainer_id": game["leader_id"],
        "explainer_name": game["leader_name"],
        "likers": set(),
        "claim_taken": False,
    }

    prev_name = game["leader_name"]
    game["winner_id"] = user.id
    game["winner_name"] = display_name
    game["claim_open"] = True
    game["claim_open_at"] = time.time()
    game["pending_round_id"] = round_id
    game["leader_id"] = None  # no leader during the claim window

    await update.message.reply_text(
        f"🎉 <b>{display_name}</b> вгадав(ла) слово: <b>{word}</b>!\n"
        f"Пояснював(ла): <b>{prev_name}</b>\n\n"
        f"🎯 Натисніть «Хочу пояснювати», щоб стати наступним.\n"
        f"Перші {CLAIM_PRIORITY_SECONDS} с — пріоритет у переможця, далі може будь-хто.\n\n"
        f"👍 А ще можна подякувати лайком за пояснення — кнопка залишиться і після передачі ходу.",
        parse_mode="HTML",
        reply_markup=_claim_keyboard(round_id, 0),
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
