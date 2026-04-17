import os
import time
import random
import json
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


# Apostrophes we want to treat as equivalent / ignore when comparing guesses.
_APOSTROPHES = "'\u2019\u02bc\u02b9\u2018`\u00b4"


_SEPARATORS = "- \t\u2010\u2011\u2012\u2013\u2014\u2015"


def full_name(user) -> str:
    """Return user's full name (first + last if available)."""
    if user.last_name:
        return f"{user.first_name} {user.last_name}"
    return user.first_name


def normalize(text: str) -> str:
    """Lowercase and strip apostrophes, hyphens, and spaces for guess comparison."""
    text = text.strip().lower()
    for ch in _APOSTROPHES:
        text = text.replace(ch, "")
    for ch in _SEPARATORS:
        text = text.replace(ch, "")
    return text


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
DB_PATH = Path(os.getenv("DB_PATH", Path(__file__).parent / "rating.db"))


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
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS game_state (
            chat_id  INTEGER PRIMARY KEY,
            data     TEXT NOT NULL
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


def get_top(chat_id: int) -> list[tuple[str, int, int]]:
    conn = _db()
    rows = conn.execute(
        "SELECT username, score, likes FROM ratings WHERE chat_id = ? "
        "ORDER BY score DESC, likes DESC",
        (chat_id,),
    ).fetchall()
    conn.close()
    return rows


def _game_to_json(game: dict) -> str:
    """Serialize game state to JSON for DB storage."""
    d = {
        "word": game.get("word"),
        "leader_id": game.get("leader_id"),
        "leader_name": game.get("leader_name"),
        "active": game.get("active", False),
        "claim_open": game.get("claim_open", False),
        "winner_id": game.get("winner_id"),
        "winner_name": game.get("winner_name"),
        "claim_open_at": game.get("claim_open_at", 0.0),
        "pending_round_id": game.get("pending_round_id"),
        "next_round_id": game.get("next_round_id", 1),
        "used_words": list(game.get("used_words", set())),
        "rounds": {
            str(k): {
                "explainer_id": v["explainer_id"],
                "explainer_name": v["explainer_name"],
                "likers": list(v["likers"]),
                "claim_taken": v["claim_taken"],
            }
            for k, v in game.get("rounds", {}).items()
        },
    }
    return json.dumps(d, ensure_ascii=False)


def _game_from_json(raw: str) -> dict:
    """Deserialize game state from JSON."""
    d = json.loads(raw)
    d["used_words"] = set(d.get("used_words", []))
    d["timeout_job"] = None
    rounds = {}
    for k, v in d.get("rounds", {}).items():
        rounds[int(k)] = {
            "explainer_id": v["explainer_id"],
            "explainer_name": v["explainer_name"],
            "likers": set(v["likers"]),
            "claim_taken": v["claim_taken"],
        }
    d["rounds"] = rounds
    return d


def save_game(chat_id: int, game: dict) -> None:
    """Persist current game state to the database."""
    conn = _db()
    conn.execute(
        "INSERT OR REPLACE INTO game_state (chat_id, data) VALUES (?, ?)",
        (chat_id, _game_to_json(game)),
    )
    conn.commit()
    conn.close()


def delete_game_state(chat_id: int) -> None:
    """Remove persisted game state when a game ends."""
    conn = _db()
    conn.execute("DELETE FROM game_state WHERE chat_id = ?", (chat_id,))
    conn.commit()
    conn.close()


def load_all_games() -> dict[int, dict]:
    """Load all persisted game states from the database."""
    conn = _db()
    rows = conn.execute("SELECT chat_id, data FROM game_state").fetchall()
    conn.close()
    result: dict[int, dict] = {}
    for chat_id, data in rows:
        try:
            game = _game_from_json(data)
            if game.get("active"):
                result[chat_id] = game
            else:
                delete_game_state(chat_id)
        except Exception:
            logger.warning("Failed to restore game for chat %s, removing", chat_id)
            delete_game_state(chat_id)
    return result


# ---------------------------------------------------------------------------
# Game state (per-chat, persisted via SQLite)
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
    delete_game_state(chat_id)

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
        "leader_name": full_name(user),
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

    save_game(chat_id, games[chat_id])
    schedule_timeout(context, chat_id)

    await update.message.reply_text(
        f"🐊 Гру розпочато!\n\n"
        f"Загадує: <b>{full_name(user)}</b>\n"
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
        delete_game_state(chat_id)
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
    user_name = full_name(query.from_user)

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
        save_game(chat_id, game)
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
        save_game(chat_id, game)
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

        # The previous explainer cannot claim the next round immediately.
        if user_id == round_data["explainer_id"]:
            await query.answer(
                "Ви щойно пояснювали — пропустіть хоча б 1 раунд 😉",
                show_alert=True,
            )
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

        save_game(chat_id, game)
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

    if normalize(update.message.text) != normalize(game["word"]):
        return

    # ---- Correct guess ----
    word = game["word"]
    display_name = full_name(user)
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

    save_game(chat_id, game)

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
async def post_init(app: Application) -> None:
    """Restore active games from DB after bot restart."""
    global games
    restored = load_all_games()
    if restored:
        games.update(restored)
        for chat_id, game in restored.items():
            if game.get("active"):
                game["timeout_job"] = app.job_queue.run_once(
                    round_timeout_job, when=ROUND_TIMEOUT_SECONDS, chat_id=chat_id
                )
        logger.info("Restored %d active game(s) from database", len(restored))


def main() -> None:
    load_words()

    token = os.getenv("BOT_TOKEN")
    if not token:
        raise RuntimeError("BOT_TOKEN not set. Create .env file with BOT_TOKEN=<your token>")

    app = Application.builder().token(token).post_init(post_init).build()

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("stop", cmd_stop))
    app.add_handler(CommandHandler("rating", cmd_rating))
    app.add_handler(CallbackQueryHandler(button_handler))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, guess_handler))

    logger.info("Bot started")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
