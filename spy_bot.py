"""Telegram-бот для игры «Шпион» (aiogram 3)."""
import asyncio
import html
import logging
import os
import random
import re
from collections import Counter
from dataclasses import dataclass, field

from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ChatMemberStatus, ChatType, ParseMode
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.types import (
    BotCommand,
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

from storage import Storage
from words import CUSTOM, MIX, THEME_ORDER, theme_words

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("spybot")


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, default))
    except (TypeError, ValueError):
        return default


API_TOKEN = os.getenv("API_TOKEN")
DATABASE_URL = os.getenv("DATABASE_URL")
DB_PATH = os.getenv("DB_PATH", "spybot.db")
MIN_PLAYERS = max(3, _env_int("MIN_PLAYERS", 3))

# Настройки по умолчанию; каждый чат может поменять их через /settings
DEFAULT_SETTINGS = {
    "registration": _env_int("REGISTRATION_SECONDS", 180),
    "discussion": _env_int("DISCUSSION_SECONDS", 120),
    "voting": _env_int("VOTING_SECONDS", 60),
    "max_rounds": max(1, _env_int("MAX_ROUNDS", 5)),
    "spies": 0,       # 0 — авто (1 шпион на 5 игроков)
    "hint": True,     # подсказка шпиону со второго раунда
    "theme": MIX,
}
SETTING_CHOICES = {
    "theme": THEME_ORDER,
    "discussion": [60, 90, 120, 180, 240, 300],
    "voting": [30, 45, 60, 90, 120],
    "registration": [60, 120, 180, 300],
    "max_rounds": [3, 5, 7, 10],
    "spies": [0, 1, 2, 3],
    "hint": [True, False],
}
SETTING_LABELS = {
    "theme": "🎯 Тема",
    "discussion": "🗣 Обсуждение",
    "voting": "🗳 Голосование",
    "registration": "📝 Набор",
    "max_rounds": "🔁 Раундов",
    "spies": "🕵️ Шпионов",
    "hint": "💡 Подсказка шпиону",
}

SPY, CIVIL = "шпион", "мирный"
GROUP_TYPES = {ChatType.GROUP, ChatType.SUPERGROUP}
MAX_CUSTOM_WORDS = 500


def fmt_seconds(seconds: int) -> str:
    if seconds >= 60 and seconds % 60 == 0:
        return f"{seconds // 60} мин"
    if seconds > 60:
        return f"{seconds // 60} мин {seconds % 60} сек"
    return f"{seconds} сек"


def fmt_setting(key: str, value) -> str:
    if key in ("discussion", "voting", "registration"):
        return fmt_seconds(value)
    if key == "spies":
        return "авто" if not value else str(value)
    if key == "hint":
        return "вкл" if value else "выкл"
    return str(value)


RULES_TEXT = """🎲 <b>Правила игры «Шпион»</b>

1. Игроки — мирные жители и шпионы. Мирные получают в личку секретное слово, шпионы его не знают, но знают друг друга. Тема слова известна всем.
2. Каждый раунд:
   • <b>Обсуждение</b>: задавайте вопросы так, чтобы свои поняли, а шпион — нет.
   • <b>Голосование</b>: выбираем подозреваемого. Голос можно менять, за себя нельзя.
   • Набравший больше всех голосов выбывает, его роль раскрывается. При ничьей или если пропустивших не меньше — никто не выбывает.
3. Шпион во время обсуждения может отправить боту в личку <code>/guess слово</code>. Попытка одна: угадал — шпионы побеждают, ошибся — выбывает.
4. Со второго раунда шпион получает подсказку (если включена).
5. Мирные побеждают, когда раскрыты все шпионы. Шпионы — если угадали слово, если их не меньше, чем мирных, или если продержались все раунды.
6. В конце игры бот называет лучшего детектива — того, кто чаще голосовал за шпионов.

<b>Игра:</b> /game [тема] — набор, /ready — старт, /vote — к голосованию, /end — стоп, /role — моя роль.
<b>Чат:</b> /settings — настройки, /top — рейтинг, /stats — моя статистика, /words, /addword, /delword — свои слова.
Управляют игрой ведущий (кто начал набор) и админы чата."""


@dataclass
class Game:
    chat_id: int
    host_id: int
    cfg: dict
    theme: str = MIX
    phase: str = "registration"  # registration | starting | discussion | voting | tally | finished
    players: dict = field(default_factory=dict)      # живые игроки: id -> имя
    all_players: dict = field(default_factory=dict)  # все участники партии: id -> имя
    roles: dict = field(default_factory=dict)        # id -> SPY / CIVIL
    word: str = ""
    word_theme: str = ""
    round_no: int = 0
    votes: dict = field(default_factory=dict)        # голосующий -> цель (None = пропуск)
    correct_votes: dict = field(default_factory=dict)  # id -> сколько раз голосовал за шпиона
    hint_sent: bool = False
    registration_message_id: int | None = None
    voting_message_id: int | None = None
    start_now: asyncio.Event = field(default_factory=asyncio.Event)
    skip_discussion: asyncio.Event = field(default_factory=asyncio.Event)
    all_voted: asyncio.Event = field(default_factory=asyncio.Event)
    task: asyncio.Task | None = None

    def alive_spies(self) -> list:
        return [uid for uid in self.players if self.roles.get(uid) == SPY]

    def alive_civils(self) -> list:
        return [uid for uid in self.players if self.roles.get(uid) == CIVIL]

    def snapshot(self) -> dict:
        return {
            "chat_id": self.chat_id, "host_id": self.host_id, "cfg": self.cfg, "theme": self.theme,
            "players": {str(k): v for k, v in self.players.items()},
            "all_players": {str(k): v for k, v in self.all_players.items()},
            "roles": {str(k): v for k, v in self.roles.items()},
            "word": self.word, "word_theme": self.word_theme, "round_no": self.round_no,
            "correct_votes": {str(k): v for k, v in self.correct_votes.items()},
            "hint_sent": self.hint_sent, "registration_message_id": self.registration_message_id,
        }

    @classmethod
    def from_snapshot(cls, data: dict) -> "Game":
        ints = lambda d: {int(k): v for k, v in (d or {}).items()}  # noqa: E731
        roles = ints(data.get("roles"))
        return cls(
            phase="starting" if roles else "registration",
            chat_id=data["chat_id"], host_id=data["host_id"], cfg={**DEFAULT_SETTINGS, **data.get("cfg", {})},
            theme=data.get("theme", MIX), players=ints(data.get("players")),
            all_players=ints(data.get("all_players")), roles=roles,
            word=data.get("word", ""), word_theme=data.get("word_theme", ""), round_no=data.get("round_no", 0),
            correct_votes=ints(data.get("correct_votes")), hint_sent=data.get("hint_sent", False),
            registration_message_id=data.get("registration_message_id"),
        )


games: dict[int, Game] = {}        # chat_id -> игра (в каждом чате своя)
player_game: dict[int, int] = {}   # user_id -> chat_id игры, в которой он участвует
bot: Bot | None = None
storage: Storage | None = None
BOT_USERNAME = ""
router = Router()


# ====== Вспомогательные функции ======

def mention(uid: int, name: str) -> str:
    return f'<a href="tg://user?id={uid}">{html.escape(name)}</a>'


def normalize(text: str) -> str:
    return re.sub(r"\s+", " ", text.lower().replace("ё", "е")).strip(" .!?\"'«»")


async def db(method: str, *args, default=None):
    """Вызов хранилища, который никогда не роняет игру."""
    if storage is None:
        return default
    try:
        return await getattr(storage, method)(*args)
    except Exception:
        log.exception("Ошибка хранилища в %s", method)
        return default


async def load_settings(chat_id: int) -> dict:
    saved = await db("get_settings", chat_id, default={}) or {}
    cfg = {**DEFAULT_SETTINGS, **{k: v for k, v in saved.items() if k in DEFAULT_SETTINGS}}
    if cfg["theme"] not in THEME_ORDER:
        cfg["theme"] = MIX
    return cfg


async def persist(game: Game) -> None:
    if game.phase != "finished":
        await db("save_snapshot", game.chat_id, game.snapshot())


async def send(chat_id: int, text: str, **kwargs):
    try:
        return await bot.send_message(chat_id, text, **kwargs)
    except TelegramAPIError as e:
        log.warning("send_message(%s) failed: %s", chat_id, e)
        return None


async def safe_dm(uid: int, text: str) -> bool:
    try:
        await bot.send_message(uid, text)
        return True
    except TelegramAPIError as e:
        log.info("Не удалось написать игроку %s: %s", uid, e)
        return False


async def safe_edit(chat_id: int, message_id: int | None, text: str, markup=None) -> None:
    if message_id is None:
        return
    try:
        await bot.edit_message_text(text=text, chat_id=chat_id, message_id=message_id, reply_markup=markup)
    except TelegramBadRequest as e:
        if "not modified" not in str(e):
            log.warning("edit_message_text failed: %s", e)
    except TelegramAPIError as e:
        log.warning("edit_message_text failed: %s", e)


async def wait_event(event: asyncio.Event, timeout: float) -> bool:
    """True — событие наступило, False — вышло время."""
    try:
        await asyncio.wait_for(event.wait(), timeout)
        return True
    except asyncio.TimeoutError:
        return False


async def is_admin(chat_id: int, uid: int) -> bool:
    try:
        member = await bot.get_chat_member(chat_id, uid)
    except TelegramAPIError:
        return False
    return member.status in {ChatMemberStatus.CREATOR, ChatMemberStatus.ADMINISTRATOR}


async def can_control(game: Game, uid: int) -> bool:
    return uid == game.host_id or await is_admin(game.chat_id, uid)


async def cleanup(game: Game) -> None:
    if games.get(game.chat_id) is game:
        del games[game.chat_id]
    for uid in set(game.players) | set(game.all_players):
        if player_game.get(uid) == game.chat_id:
            del player_game[uid]
    await db("delete_snapshot", game.chat_id)


def cancel_task(game: Game) -> None:
    if game.task and game.task is not asyncio.current_task() and not game.task.done():
        game.task.cancel()


def spy_count(players: int, setting: int) -> int:
    wanted = setting or max(1, players // 5)
    return max(1, min(wanted, (players - 1) // 2))


def roles_summary(game: Game) -> str:
    lines = []
    for uid, name in game.all_players.items():
        icon = "🕵️" if game.roles.get(uid) == SPY else "🕊️"
        status = "" if uid in game.players else " (выбыл)"
        lines.append(f"{icon} {mention(uid, name)} — {game.roles.get(uid, '?')}{status}")
    return "\n".join(lines)


def detective_line(game: Game) -> str:
    scores = {uid: c for uid, c in game.correct_votes.items() if c > 0 and game.roles.get(uid) == CIVIL}
    if not scores:
        return ""
    best = max(scores.values())
    names = [mention(uid, game.all_players[uid]) for uid, c in scores.items() if c == best]
    return f"\n\n🔎 Лучший детектив: {', '.join(names)} ({best} точн. гол.)"


# ====== Тексты и клавиатуры ======

def registration_text(game: Game, closed: str | None = None) -> str:
    lines = ["🎮 <b>Набор в игру «Шпион»</b>", f"🎯 Тема: <b>{html.escape(game.theme)}</b>"]
    if closed:
        lines.append(closed)
    else:
        lines.append(f"Жмите «Играть». Нужно минимум {MIN_PLAYERS} игрока.")
        lines.append(f"Старт через {fmt_seconds(game.cfg['registration'])} или раньше — командой /ready.")
    lines.append("")
    lines.append(f"👥 Игроки ({len(game.players)}):")
    lines += [f"• {mention(uid, name)}" for uid, name in game.players.items()] or ["пока никого"]
    return "\n".join(lines)


def registration_markup() -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(text="🎲 Играть", callback_data="join"),
         InlineKeyboardButton(text="🚪 Выйти", callback_data="leave")],
        [InlineKeyboardButton(text="🎯 Сменить тему (ведущий)", callback_data="theme")],
    ]
    if BOT_USERNAME:
        rows.append([InlineKeyboardButton(text="🤖 Открыть бота (нужно для роли)",
                                          url=f"https://t.me/{BOT_USERNAME}?start=join")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def role_text(game: Game, uid: int) -> str:
    if game.roles.get(uid) == SPY:
        partners = [mention(p, game.all_players[p]) for p in game.roles if game.roles[p] == SPY and p != uid]
        theme = "" if game.theme == MIX else f"\nТема игры: <b>{html.escape(game.theme)}</b>."
        text = ("🕵️ Ты <b>шпион</b>! Слова ты не знаешь." + theme + "\n"
                "Слушай обсуждение и не выдай себя. Догадался — отправь мне <code>/guess слово</code> "
                "во время обсуждения. Попытка одна: промах — выбываешь.")
        if partners:
            text += "\n\n🤝 Твои напарники: " + ", ".join(partners)
        return text
    return (f"🕊️ Ты <b>мирный</b>. Слово: <b>{html.escape(game.word)}</b>\n"
            "Задавай вопросы так, чтобы свои поняли, что ты в теме, а шпион — нет.")


def hint_text(game: Game) -> str:
    letters = sum(ch.isalpha() for ch in game.word)
    return (f"💡 <b>Подсказка</b>: тема слова — «{html.escape(game.word_theme)}», "
            f"букв в нём {letters}, начинается на «{html.escape(game.word[0].upper())}».")


def voting_text(game: Game, final: bool = False) -> str:
    counts = Counter(t for t in game.votes.values() if t is not None)
    skips = sum(1 for t in game.votes.values() if t is None)
    head = "🗳 <b>Голосование завершено</b>" if final else (
        f"🗳 <b>Кто шпион?</b> На голосование {fmt_seconds(game.cfg['voting'])}, голос можно менять.")
    lines = [head, ""]
    lines += [f"• {mention(uid, name)} — {counts.get(uid, 0)}" for uid, name in game.players.items()]
    lines.append(f"• Пропустили — {skips}")
    if not final:
        waiting = [mention(uid, name) for uid, name in game.players.items() if uid not in game.votes]
        lines.append("")
        lines.append("⏳ Ещё не голосовали: " + ", ".join(waiting) if waiting else "✅ Все проголосовали.")
    return "\n".join(lines)


def voting_markup(game: Game) -> InlineKeyboardMarkup:
    buttons = [InlineKeyboardButton(text=name[:30], callback_data=f"vote:{uid}") for uid, name in game.players.items()]
    rows = [buttons[i:i + 2] for i in range(0, len(buttons), 2)]
    rows.append([InlineKeyboardButton(text="🤷 Пропустить", callback_data="vote:skip")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def settings_text(cfg: dict) -> str:
    lines = ["⚙️ <b>Настройки игры в этом чате</b>", "Нажмите на пункт, чтобы переключить значение.", ""]
    lines += [f"{SETTING_LABELS[k]}: <b>{html.escape(fmt_setting(k, cfg[k]))}</b>" for k in SETTING_CHOICES]
    lines.append("\nИзменения действуют со следующей игры.")
    return "\n".join(lines)


def settings_markup(cfg: dict) -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton(text=f"{SETTING_LABELS[k]}: {fmt_setting(k, cfg[k])}", callback_data=f"set:{k}")]
            for k in SETTING_CHOICES]
    rows.append([InlineKeyboardButton(text="✅ Готово", callback_data="set:close")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def next_choice(options: list, current):
    try:
        return options[(options.index(current) + 1) % len(options)]
    except ValueError:
        return options[0]


# ====== Ход игры ======

async def record_stats(game: Game, winner: str) -> None:
    results = [{
        "user_id": uid, "name": name, "won": game.roles.get(uid) == winner,
        "spy": game.roles.get(uid) == SPY, "correct_votes": game.correct_votes.get(uid, 0),
    } for uid, name in game.all_players.items()]
    await db("record_results", game.chat_id, results)


async def finish_game(game: Game, text: str, winner: str) -> None:
    if game.phase == "finished":
        return
    if game.phase == "voting":
        await safe_edit(game.chat_id, game.voting_message_id, voting_text(game, final=True))
    game.phase = "finished"
    await cleanup(game)
    await record_stats(game, winner)
    await send(game.chat_id,
               f"{text}\n\n🔑 Слово было: <b>{html.escape(game.word)}</b> ({html.escape(game.word_theme)})"
               f"\n\n🧾 Роли:\n{roles_summary(game)}{detective_line(game)}\n\n📊 Рейтинг чата: /top")
    result = "победа шпионов 🕵️" if winner == SPY else "победа мирных 🕊️"
    for uid in game.all_players:
        verdict = "Ты выиграл! 🎉" if game.roles.get(uid) == winner else "Ты проиграл."
        await safe_dm(uid, f"🏁 Игра окончена: {result}. {verdict}")
    cancel_task(game)


async def stop_game(game: Game, text: str) -> None:
    if game.phase == "finished":
        return
    was_registration = not game.roles
    if game.phase == "voting":
        await safe_edit(game.chat_id, game.voting_message_id, voting_text(game, final=True))
    game.phase = "finished"
    cancel_task(game)
    await cleanup(game)
    if was_registration:
        await safe_edit(game.chat_id, game.registration_message_id, registration_text(game, closed="🛑 Набор отменён."))
        await send(game.chat_id, text)
        return
    await send(game.chat_id,
               f"{text}\n\n🔑 Слово было: <b>{html.escape(game.word)}</b>\n\n🧾 Роли:\n{roles_summary(game)}")
    for uid in game.all_players:
        await safe_dm(uid, "🛑 Игра остановлена.")


async def check_win(game: Game) -> bool:
    spies, civils = len(game.alive_spies()), len(game.alive_civils())
    if spies == 0:
        await finish_game(game, "🎉 Все шпионы раскрыты. <b>Победа мирных!</b>", CIVIL)
        return True
    if spies >= civils:
        await finish_game(game, "🕵️ Шпионов не меньше, чем мирных. <b>Победа шпионов!</b>", SPY)
        return True
    return False


async def pick_word(game: Game) -> bool:
    custom = await db("list_words", game.chat_id, default=[]) or []
    pool = theme_words(game.theme, custom)
    if not pool:
        await send(game.chat_id, f"В теме «{html.escape(game.theme)}» нет слов — играем «{MIX}». "
                                 "Свои слова добавляются командой /addword.")
        game.theme = MIX
        pool = theme_words(MIX, custom)
    game.word, game.word_theme = random.choice(pool)
    return True


async def assign_roles(game: Game) -> None:
    ids = list(game.players)
    count = spy_count(len(ids), game.cfg["spies"])
    spies = set(random.sample(ids, count))
    await pick_word(game)
    game.all_players = dict(game.players)
    game.roles = {uid: (SPY if uid in spies else CIVIL) for uid in ids}
    await persist(game)

    failed = [uid for uid in ids if not await safe_dm(uid, role_text(game, uid))]
    text = (f"🎭 Роли розданы! Тема: <b>{html.escape(game.theme)}</b>. "
            f"Шпионов в игре: <b>{count}</b>. Проверьте личку с ботом.")
    if failed:
        text += ("\n\n❗ Не получили роль: " + ", ".join(mention(u, game.players[u]) for u in failed)
                 + ". Откройте бота и отправьте ему /role.")
    await send(game.chat_id, text)


async def discussion_phase(game: Game) -> None:
    game.phase = "discussion"
    game.skip_discussion.clear()
    await persist(game)
    seconds = game.cfg["discussion"]
    names = "\n".join(f"• {mention(uid, name)}" for uid, name in game.players.items())
    await send(game.chat_id,
               f"🔁 <b>Раунд {game.round_no} из {game.cfg['max_rounds']}</b>\n"
               f"👥 В игре ({len(game.players)}):\n{names}\n\n"
               f"🗣 Обсуждение — {fmt_seconds(seconds)}.\n"
               "Шпион может угадать слово: <code>/guess слово</code> в личку боту.\n"
               "Ведущий может сразу перейти к голосованию: /vote")
    if game.round_no >= 2 and game.cfg["hint"] and not game.hint_sent:
        game.hint_sent = True
        for uid in game.alive_spies():
            await safe_dm(uid, hint_text(game))
        await persist(game)
    warn_before = 30
    if seconds > warn_before * 2:
        if await wait_event(game.skip_discussion, seconds - warn_before):
            return
        await send(game.chat_id, f"⏳ Осталось {warn_before} секунд обсуждения!")
        await wait_event(game.skip_discussion, warn_before)
    else:
        await wait_event(game.skip_discussion, seconds)


async def voting_phase(game: Game) -> None:
    game.votes = {}
    game.all_voted.clear()
    game.phase = "voting"
    msg = await send(game.chat_id, voting_text(game), reply_markup=voting_markup(game))
    game.voting_message_id = msg.message_id if msg else None
    await wait_event(game.all_voted, game.cfg["voting"])
    game.phase = "tally"


async def resolve_voting(game: Game) -> bool:
    """Подводит итог голосования. True — игра закончилась."""
    await safe_edit(game.chat_id, game.voting_message_id, voting_text(game, final=True))
    for voter, target in game.votes.items():
        if target is not None and game.roles.get(target) == SPY:
            game.correct_votes[voter] = game.correct_votes.get(voter, 0) + 1
    counts = Counter(t for t in game.votes.values() if t is not None)
    skips = sum(1 for t in game.votes.values() if t is None)

    if not counts:
        await send(game.chat_id, "🤷 Никто ни за кого не проголосовал — никто не выбывает.")
        return False
    ranking = counts.most_common()
    top_votes = ranking[0][1]
    leaders = [uid for uid, c in ranking if c == top_votes]
    if len(leaders) > 1:
        await send(game.chat_id, "🤝 Ничья — никто не выбывает.")
        return False
    if skips >= top_votes:
        await send(game.chat_id, "🤷 Пропустивших не меньше, чем голосов против лидера — никто не выбывает.")
        return False

    out = leaders[0]
    name = game.players.pop(out)
    role = game.roles[out]
    icon = "🕵️ Это был шпион!" if role == SPY else "🕊️ Это был мирный житель."
    await send(game.chat_id, f"❌ Выбывает {mention(out, name)} ({top_votes} гол.). {icon}")
    await safe_dm(out, "Ты выбыл. Можешь следить за игрой, но не подсказывай 🤐")
    if await check_win(game):
        return True
    await persist(game)
    return False


async def run_game(game: Game, resumed: bool = False) -> None:
    try:
        if not game.roles:
            if resumed:
                await send(game.chat_id, "♻️ Бот перезапускался — набор продолжается. /ready — начать.")
            await wait_event(game.start_now, game.cfg["registration"])
            game.phase = "starting"
            if len(game.players) < MIN_PLAYERS:
                await stop_game(game, f"❗ Недостаточно игроков (нужно минимум {MIN_PLAYERS}). Набор закрыт.")
                return
            await safe_edit(game.chat_id, game.registration_message_id,
                            registration_text(game, closed="✅ Набор закрыт, игра началась!"))
            await assign_roles(game)
        else:
            game.round_no = max(0, game.round_no - 1)  # переигрываем прерванный раунд
            await send(game.chat_id, f"♻️ Бот перезапускался. Продолжаем игру с раунда {game.round_no + 1}!")

        while game.phase != "finished":
            game.round_no += 1
            await discussion_phase(game)
            if game.phase == "finished":
                return
            await voting_phase(game)
            if await resolve_voting(game):
                return
            if game.round_no >= game.cfg["max_rounds"]:
                await finish_game(game, f"⌛ Шпионы продержались {game.round_no} раундов. <b>Победа шпионов!</b>", SPY)
                return
    except asyncio.CancelledError:
        raise
    except Exception:
        log.exception("Ошибка в игре чата %s", game.chat_id)
        game.phase = "finished"
        await cleanup(game)
        await send(game.chat_id, "⚠️ Внутренняя ошибка, игра остановлена. Начните заново: /game")


def start_game_task(game: Game, resumed: bool = False) -> None:
    game.task = asyncio.create_task(run_game(game, resumed))


async def restore_games() -> None:
    for data in await db("load_snapshots", default=[]) or []:
        try:
            game = Game.from_snapshot(data)
        except Exception:
            log.exception("Не удалось восстановить игру: %s", data.get("chat_id"))
            continue
        if game.chat_id in games:
            continue
        games[game.chat_id] = game
        for uid in game.all_players or game.players:
            player_game[uid] = game.chat_id
        start_game_task(game, resumed=True)
        log.info("Восстановлена игра в чате %s", game.chat_id)


# ====== Команды: игра ======

@router.message(CommandStart(), F.chat.type == ChatType.PRIVATE)
async def cmd_start_private(message: Message):
    await message.answer(
        "👋 Привет! Я веду игру «Шпион».\n\n"
        "Добавь меня в группу и напиши там /game.\n"
        "Если ты уже нажимал «Играть» — вернись в группу и нажми ещё раз, теперь я смогу прислать тебе роль.\n\n"
        "/rules — правила, /stats — твоя статистика")


def match_theme(arg: str | None) -> str | None:
    if not arg:
        return None
    arg = normalize(arg)
    for theme in THEME_ORDER:
        if normalize(theme).startswith(arg):
            return theme
    return None


@router.message(Command("game", "start"), F.chat.type.in_(GROUP_TYPES))
async def cmd_game(message: Message, command: CommandObject | None = None):
    chat_id = message.chat.id
    if chat_id in games:
        await message.reply("Игра в этом чате уже идёт. Остановить: /end")
        return
    cfg = await load_settings(chat_id)
    theme = match_theme(command.args if command else None) or cfg["theme"]
    game = Game(chat_id=chat_id, host_id=message.from_user.id, cfg=cfg, theme=theme)
    games[chat_id] = game
    sent = await message.answer(registration_text(game), reply_markup=registration_markup())
    game.registration_message_id = sent.message_id
    await persist(game)
    start_game_task(game)


@router.message(Command("game", "ready", "end", "vote", "settings", "top", "addword", "delword", "words"),
                F.chat.type == ChatType.PRIVATE)
async def cmd_group_only(message: Message):
    await message.answer("Эта команда работает в группе.")


@router.message(Command("ready"))
async def cmd_ready(message: Message):
    game = games.get(message.chat.id)
    if not game or game.phase != "registration":
        await message.reply("❌ Сейчас нет открытого набора. Начать: /game")
        return
    if not await can_control(game, message.from_user.id):
        await message.reply("Начать игру может ведущий или админ чата.")
        return
    if len(game.players) < MIN_PLAYERS:
        await message.reply(f"Пока игроков {len(game.players)}, нужно минимум {MIN_PLAYERS}.")
        return
    game.start_now.set()


@router.message(Command("vote"))
async def cmd_vote(message: Message):
    game = games.get(message.chat.id)
    if not game or game.phase != "discussion":
        await message.reply("Сейчас не идёт обсуждение.")
        return
    if not await can_control(game, message.from_user.id):
        await message.reply("Перейти к голосованию может ведущий или админ чата.")
        return
    game.skip_discussion.set()


@router.message(Command("end"))
async def cmd_end(message: Message):
    game = games.get(message.chat.id)
    if not game:
        await message.reply("❌ Сейчас нет активной игры.")
        return
    if not await can_control(game, message.from_user.id):
        await message.reply("Остановить игру может ведущий или админ чата.")
        return
    await stop_game(game, "🛑 Игра остановлена.")


@router.message(Command("rules"))
async def cmd_rules(message: Message):
    await message.answer(RULES_TEXT)


@router.message(Command("chatid"))
async def cmd_chatid(message: Message):
    await message.reply(f"ID этого чата: <code>{message.chat.id}</code>, тип: {message.chat.type}")


@router.message(Command("role"))
async def cmd_role(message: Message):
    if message.chat.type != ChatType.PRIVATE:
        await message.reply("🤫 Роль пришлю только в личку — напиши мне /role там.")
        return
    uid = message.from_user.id
    game = games.get(player_game.get(uid, 0))
    if not game or not game.roles or uid not in game.roles:
        await message.answer("Ты сейчас не участвуешь в игре, или роли ещё не розданы.")
        return
    text = role_text(game, uid)
    if game.roles[uid] == SPY and game.hint_sent:
        text += "\n\n" + hint_text(game)
    await message.answer(text)


@router.message(Command("guess"))
async def cmd_guess(message: Message, command: CommandObject):
    if message.chat.type != ChatType.PRIVATE:
        await message.reply("🤫 Догадку отправляй мне в личку: /guess слово")
        return
    uid = message.from_user.id
    game = games.get(player_game.get(uid, 0))
    if not game or uid not in game.players or game.roles.get(uid) != SPY:
        await message.answer("Угадывать слово может только шпион, который ещё в игре.")
        return
    if game.phase != "discussion":
        await message.answer("Угадывать можно только во время обсуждения.")
        return
    guess = (command.args or "").strip()
    if not guess:
        await message.answer("Напиши так: <code>/guess слово</code>")
        return

    name = game.players[uid]
    if normalize(guess) == normalize(game.word):
        await message.answer("🎯 В точку!")
        await finish_game(game, f"🎯 Шпион {mention(uid, name)} угадал слово! <b>Победа шпионов!</b>", SPY)
        return

    game.players.pop(uid)
    await message.answer("❌ Мимо. Ты выбываешь.")
    await send(game.chat_id,
               f"❌ Шпион {mention(uid, name)} попытался угадать слово («{html.escape(guess)}») и ошибся. Он выбывает!")
    if not await check_win(game):
        await persist(game)


# ====== Команды: чат ======

@router.message(Command("settings"))
async def cmd_settings(message: Message):
    if not await is_admin(message.chat.id, message.from_user.id):
        await message.reply("Настройки меняют админы чата.")
        return
    cfg = await load_settings(message.chat.id)
    await message.answer(settings_text(cfg), reply_markup=settings_markup(cfg))


@router.message(Command("stats"))
async def cmd_stats(message: Message):
    target = message.from_user
    if message.reply_to_message and message.reply_to_message.from_user:
        target = message.reply_to_message.from_user
    in_group = message.chat.type in GROUP_TYPES
    st = await db("user_stats", target.id, message.chat.id if in_group else None)
    where = "в этом чате" if in_group else "во всех чатах"
    if not st:
        await message.reply(f"У {html.escape(target.full_name)} пока нет сыгранных игр {where}.")
        return
    winrate = round(100 * st["wins"] / st["games"])
    await message.reply(
        f"📊 <b>{html.escape(target.full_name)}</b> — статистика {where}\n"
        f"Игр: {st['games']}, побед: {st['wins']} ({winrate}%)\n"
        f"Шпионом: {st['spy_games']} (побед: {st['spy_wins']})\n"
        f"🔎 Точных голосов против шпионов: {st['correct_votes']}")


@router.message(Command("top"))
async def cmd_top(message: Message):
    rows = await db("top", message.chat.id, 10, default=[]) or []
    if not rows:
        await message.reply("В этом чате ещё не доиграли ни одной игры.")
        return
    medals = ["🥇", "🥈", "🥉"]
    lines = ["🏆 <b>Топ игроков чата</b>", ""]
    for i, r in enumerate(rows):
        place = medals[i] if i < 3 else f"{i + 1}."
        lines.append(f"{place} {html.escape(r['name'])} — {r['wins']} побед из {r['games']} "
                     f"(🕵️ {r['spy_wins']}, 🔎 {r['correct_votes']})")
    await message.reply("\n".join(lines))


def parse_words(text: str | None) -> list[str]:
    words = []
    for part in re.split(r"[,;\n]", text or ""):
        w = re.sub(r"\s+", " ", part).strip(" .!?\"'«»")
        if 2 <= len(w) <= 40 and w.lower() not in (x.lower() for x in words):
            words.append(w)
    return words[:50]


@router.message(Command("addword"))
async def cmd_addword(message: Message, command: CommandObject):
    if not await is_admin(message.chat.id, message.from_user.id):
        await message.reply("Добавлять слова могут админы чата.")
        return
    words = parse_words(command.args)
    if not words:
        await message.reply("Напишите так: <code>/addword слово1, слово2, слово3</code>")
        return
    existing = await db("list_words", message.chat.id, default=[]) or []
    if len(existing) + len(words) > MAX_CUSTOM_WORDS:
        await message.reply(f"Слишком много: в чате может быть до {MAX_CUSTOM_WORDS} своих слов.")
        return
    await db("add_words", message.chat.id, words)
    await message.reply(f"✅ Добавлено слов: {len(words)}. Играть только ими: <code>/game свои</code>")


@router.message(Command("delword"))
async def cmd_delword(message: Message, command: CommandObject):
    if not await is_admin(message.chat.id, message.from_user.id):
        await message.reply("Удалять слова могут админы чата.")
        return
    words = parse_words(command.args)
    if not words:
        await message.reply("Напишите так: <code>/delword слово</code>")
        return
    for w in words:
        await db("remove_word", message.chat.id, w)
    await message.reply(f"🗑 Удалено: {', '.join(html.escape(w) for w in words)}")


@router.message(Command("words"))
async def cmd_words(message: Message):
    words = await db("list_words", message.chat.id, default=[]) or []
    if not words:
        await message.reply("Своих слов пока нет. Добавить: <code>/addword слово1, слово2</code>")
        return
    shown = ", ".join(html.escape(w) for w in words[:200])
    more = f" …и ещё {len(words) - 200}" if len(words) > 200 else ""
    await message.reply(f"📚 Свои слова чата ({len(words)}):\n{shown}{more}")


# ====== Кнопки ======

@router.callback_query(F.data == "join")
async def cb_join(cq: CallbackQuery):
    game = games.get(cq.message.chat.id) if cq.message else None
    if not game or game.phase != "registration":
        await cq.answer("Набор закрыт.", show_alert=True)
        return
    user = cq.from_user
    if user.id in game.players:
        await cq.answer("Вы уже в игре.")
        return
    other = player_game.get(user.id)
    if other is not None and other != game.chat_id:
        await cq.answer("Вы уже участвуете в игре в другом чате.", show_alert=True)
        return
    if not await safe_dm(user.id, "✅ Вы в игре! Роль придёт сюда, когда игра начнётся."):
        await cq.answer("Сначала откройте бота в личке (кнопка «Открыть бота» → Start), потом жмите «Играть».",
                        show_alert=True)
        return
    if game.phase != "registration":
        await cq.answer("Набор уже закрыт.", show_alert=True)
        return
    game.players[user.id] = user.full_name
    player_game[user.id] = game.chat_id
    await cq.answer("Вы зарегистрированы!")
    await safe_edit(game.chat_id, game.registration_message_id, registration_text(game), registration_markup())
    await persist(game)


@router.callback_query(F.data == "leave")
async def cb_leave(cq: CallbackQuery):
    game = games.get(cq.message.chat.id) if cq.message else None
    if not game or game.phase != "registration" or cq.from_user.id not in game.players:
        await cq.answer("Вы не в списке игроков.")
        return
    game.players.pop(cq.from_user.id)
    player_game.pop(cq.from_user.id, None)
    await cq.answer("Вы вышли из игры.")
    await safe_edit(game.chat_id, game.registration_message_id, registration_text(game), registration_markup())
    await persist(game)


@router.callback_query(F.data == "theme")
async def cb_theme(cq: CallbackQuery):
    game = games.get(cq.message.chat.id) if cq.message else None
    if not game or game.phase != "registration":
        await cq.answer("Набор закрыт.")
        return
    if not await can_control(game, cq.from_user.id):
        await cq.answer("Тему выбирает ведущий или админ.", show_alert=True)
        return
    game.theme = next_choice(THEME_ORDER, game.theme)
    await cq.answer(f"Тема: {game.theme}")
    await safe_edit(game.chat_id, game.registration_message_id, registration_text(game), registration_markup())
    await persist(game)


@router.callback_query(F.data.startswith("set:"))
async def cb_setting(cq: CallbackQuery):
    chat_id = cq.message.chat.id
    if not await is_admin(chat_id, cq.from_user.id):
        await cq.answer("Настройки меняют админы чата.", show_alert=True)
        return
    key = cq.data.split(":", 1)[1]
    if key == "close":
        await cq.answer("Сохранено")
        cfg = await load_settings(chat_id)
        await safe_edit(chat_id, cq.message.message_id, settings_text(cfg))
        return
    if key not in SETTING_CHOICES:
        await cq.answer()
        return
    cfg = await load_settings(chat_id)
    cfg[key] = next_choice(SETTING_CHOICES[key], cfg[key])
    await db("save_settings", chat_id, cfg)
    await cq.answer(f"{SETTING_LABELS[key]}: {fmt_setting(key, cfg[key])}")
    await safe_edit(chat_id, cq.message.message_id, settings_text(cfg), settings_markup(cfg))


@router.callback_query(F.data.startswith("vote:"))
async def cb_vote(cq: CallbackQuery):
    game = games.get(cq.message.chat.id) if cq.message else None
    if not game or game.phase != "voting" or cq.message.message_id != game.voting_message_id:
        await cq.answer("Это голосование уже закрыто.")
        return
    voter = cq.from_user.id
    if voter not in game.players:
        await cq.answer("Голосуют только игроки, которые ещё в игре.")
        return
    raw = cq.data.split(":", 1)[1]
    target = None if raw == "skip" else int(raw)
    if target is not None and target not in game.players:
        await cq.answer("Этот игрок уже выбыл.")
        return
    if target == voter:
        await cq.answer("За себя голосовать нельзя 🙂", show_alert=True)
        return
    changed = voter in game.votes
    game.votes[voter] = target
    await cq.answer("Голос изменён." if changed else "Голос принят.")
    if len(game.votes) >= len(game.players):
        game.all_voted.set()
    await safe_edit(game.chat_id, game.voting_message_id, voting_text(game), voting_markup(game))


# ====== Запуск ======

async def on_startup(bot: Bot):
    global BOT_USERNAME, storage
    storage = Storage(DATABASE_URL, DB_PATH)
    try:
        await storage.init()
    except Exception:
        log.exception("Хранилище недоступно — работаем без статистики и сохранения игр")
        storage = None
    me = await bot.get_me()
    BOT_USERNAME = me.username or ""
    await bot.set_my_commands([
        BotCommand(command="game", description="Начать набор (можно с темой: /game еда)"),
        BotCommand(command="ready", description="Закончить набор и начать"),
        BotCommand(command="vote", description="Перейти к голосованию"),
        BotCommand(command="end", description="Остановить игру"),
        BotCommand(command="guess", description="Шпион: угадать слово (в личку)"),
        BotCommand(command="role", description="Напомнить мою роль (в личку)"),
        BotCommand(command="top", description="Рейтинг чата"),
        BotCommand(command="stats", description="Моя статистика"),
        BotCommand(command="settings", description="Настройки игры (админы)"),
        BotCommand(command="words", description="Свои слова чата"),
        BotCommand(command="addword", description="Добавить свои слова (админы)"),
        BotCommand(command="delword", description="Удалить своё слово (админы)"),
        BotCommand(command="rules", description="Правила игры"),
    ])
    await restore_games()
    log.info("Бот @%s запущен", BOT_USERNAME)


async def on_shutdown(bot: Bot):
    if storage:
        await storage.close()


async def main():
    global bot
    if not API_TOKEN:
        raise SystemExit("Не задана переменная окружения API_TOKEN")
    bot = Bot(token=API_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = Dispatcher()
    dp.include_router(router)
    dp.startup.register(on_startup)
    dp.shutdown.register(on_shutdown)
    await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())


if __name__ == "__main__":
    asyncio.run(main())
