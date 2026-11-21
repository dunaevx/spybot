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

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("spybot")


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, default))
    except (TypeError, ValueError):
        return default


API_TOKEN = os.getenv("API_TOKEN")
REGISTRATION_SECONDS = _env_int("REGISTRATION_SECONDS", 180)
DISCUSSION_SECONDS = _env_int("DISCUSSION_SECONDS", 120)
VOTING_SECONDS = _env_int("VOTING_SECONDS", 60)
MIN_PLAYERS = max(3, _env_int("MIN_PLAYERS", 3))
MAX_ROUNDS = max(1, _env_int("MAX_ROUNDS", 5))

SPY, CIVIL = "шпион", "мирный"
GROUP_TYPES = {ChatType.GROUP, ChatType.SUPERGROUP}

WORDS = sorted(set([
    'библиотека', 'пиццерия', 'пляж', 'аэропорт', 'школа', 'больница', 'кафе', 'поезд', 'кинотеатр',
    'университет', 'парк', 'музей', 'зоопарк', 'магазин', 'метро', 'автобус', 'ресторан', 'банк',
    'офис', 'спортзал', 'тюрьма', 'театр', 'церковь', 'стадион', 'космический корабль', 'ферма',
    'гостиница', 'ночной клуб', 'свадьба', 'похороны', 'пожарная станция', 'военная база',
    'детский сад', 'гараж', 'квартира', 'лагерь', 'сауна', 'рыбалка', 'кабинет врача', 'пекарня',
    'космодром', 'пароход', 'порт', 'рынок', 'концерт', 'супермаркет', 'автостоянка', 'гора',
    'граница', 'пустыня', 'лес', 'вулкан', 'завод', 'шахта', 'конференция', 'пейнтбол', 'боулинг',
    'тренинг', 'пляжный бар', 'столовая', 'бассейн', 'клуб настольных игр', 'туристическая база',
    'участок полиции', 'космическая станция', 'крейсер', 'футбольное поле', 'гольф-клуб',
    'массажный салон', 'прачечная', 'казино', 'бункер', 'телестудия', 'радиостанция', 'суд',
    'лаборатория', 'кухня', 'цирк', 'танцпол', 'дискотека', 'киностудия', 'зоомагазин', 'шиномонтаж',
    'барбершоп', 'парикмахерская', 'винодельня', 'ремонтная мастерская', 'ателье', 'галерея',
    'аквапарк', 'игровой зал', 'заправка', 'чемпионат', 'метеостанция', 'автошкола', 'вертолётная площадка',
    'свалка', 'лифт', 'чердак', 'подвал', 'водоочистная станция', 'зоопитомник', 'обсерватория',
    'рекламное агентство', 'кафедра университета', 'читальный зал', 'садоводческое товарищество',
]))


def fmt_seconds(seconds: int) -> str:
    if seconds >= 60 and seconds % 60 == 0:
        return f"{seconds // 60} мин"
    return f"{seconds} сек"


RULES_TEXT = f"""🎲 <b>Правила игры «Шпион»</b>

1. Игроки — мирные жители и шпионы (1 шпион на каждые 5 игроков, минимум один).
2. Мирные получают в личку секретное слово (место). Шпионы слова не знают, но знают друг друга.
3. Каждый раунд:
   • <b>Обсуждение</b> ({fmt_seconds(DISCUSSION_SECONDS)}): задавайте вопросы так, чтобы свои поняли, а шпион — нет.
   • <b>Голосование</b> ({fmt_seconds(VOTING_SECONDS)}): выбираем подозреваемого. Голос можно менять, за себя голосовать нельзя.
   • Игрок с наибольшим числом голосов выбывает, его роль раскрывается. При ничьей или если большинство пропустило — никто не выбывает.
4. Шпион во время обсуждения может отправить боту в личку <code>/guess слово</code>. Одна попытка: угадал — шпионы побеждают, ошибся — выбывает.
5. Мирные побеждают, когда раскрыты все шпионы.
6. Шпионы побеждают, если угадали слово, если их стало не меньше, чем мирных, или если продержались {MAX_ROUNDS} раундов.

Команды: /game — начать набор, /ready — старт, /vote — к голосованию, /end — остановить, /role — напомнить роль.
Управляют игрой ведущий (кто начал набор) и админы чата."""


@dataclass
class Game:
    chat_id: int
    host_id: int
    phase: str = "registration"  # registration | starting | discussion | voting | tally | finished
    players: dict = field(default_factory=dict)      # живые игроки: id -> имя
    all_players: dict = field(default_factory=dict)  # все участники партии: id -> имя
    roles: dict = field(default_factory=dict)        # id -> SPY / CIVIL
    word: str = ""
    round_no: int = 0
    votes: dict = field(default_factory=dict)        # голосующий -> цель (None = пропуск)
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


games: dict[int, Game] = {}        # chat_id -> игра (в каждом чате своя)
player_game: dict[int, int] = {}   # user_id -> chat_id игры, в которой он участвует
bot: Bot | None = None
BOT_USERNAME = ""
router = Router()


# ====== Вспомогательные функции ======

def mention(uid: int, name: str) -> str:
    return f'<a href="tg://user?id={uid}">{html.escape(name)}</a>'


def normalize(text: str) -> str:
    return re.sub(r"\s+", " ", text.lower().replace("ё", "е")).strip(" .!?\"'«»")


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


async def can_control(game: Game, uid: int) -> bool:
    if uid == game.host_id:
        return True
    try:
        member = await bot.get_chat_member(game.chat_id, uid)
    except TelegramAPIError:
        return False
    return member.status in {ChatMemberStatus.CREATOR, ChatMemberStatus.ADMINISTRATOR}


def cleanup(game: Game) -> None:
    if games.get(game.chat_id) is game:
        del games[game.chat_id]
    for uid in set(game.players) | set(game.all_players):
        if player_game.get(uid) == game.chat_id:
            del player_game[uid]


def cancel_task(game: Game) -> None:
    if game.task and game.task is not asyncio.current_task() and not game.task.done():
        game.task.cancel()


def roles_summary(game: Game) -> str:
    lines = []
    for uid, name in game.all_players.items():
        icon = "🕵️" if game.roles.get(uid) == SPY else "🕊️"
        status = "" if uid in game.players else " (выбыл)"
        lines.append(f"{icon} {mention(uid, name)} — {game.roles.get(uid, '?')}{status}")
    return "\n".join(lines)


# ====== Тексты и клавиатуры ======

def registration_text(game: Game, closed: str | None = None) -> str:
    lines = ["🎮 <b>Набор в игру «Шпион»</b>"]
    if closed:
        lines.append(closed)
    else:
        lines.append(f"Жмите «Играть». Нужно минимум {MIN_PLAYERS} игрока.")
        lines.append(f"Старт через {fmt_seconds(REGISTRATION_SECONDS)} или раньше — командой /ready.")
    lines.append("")
    lines.append(f"👥 Игроки ({len(game.players)}):")
    lines += [f"• {mention(uid, name)}" for uid, name in game.players.items()] or ["пока никого"]
    return "\n".join(lines)


def registration_markup() -> InlineKeyboardMarkup:
    rows = [[
        InlineKeyboardButton(text="🎲 Играть", callback_data="join"),
        InlineKeyboardButton(text="🚪 Выйти", callback_data="leave"),
    ]]
    if BOT_USERNAME:
        rows.append([InlineKeyboardButton(text="🤖 Открыть бота (нужно для роли)",
                                          url=f"https://t.me/{BOT_USERNAME}?start=join")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def role_text(game: Game, uid: int) -> str:
    if game.roles.get(uid) == SPY:
        partners = [mention(p, game.all_players[p]) for p in game.roles if game.roles[p] == SPY and p != uid]
        text = ("🕵️ Ты <b>шпион</b>! Слова ты не знаешь.\n"
                "Слушай обсуждение и не выдай себя. Догадался — отправь мне <code>/guess слово</code> "
                "во время обсуждения. Попытка одна: промах — выбываешь.")
        if partners:
            text += "\n\n🤝 Твои напарники: " + ", ".join(partners)
        return text
    return (f"🕊️ Ты <b>мирный</b>. Слово: <b>{html.escape(game.word)}</b>\n"
            "Задавай вопросы так, чтобы свои поняли, что ты в теме, а шпион — нет.")


def voting_text(game: Game, final: bool = False) -> str:
    counts = Counter(t for t in game.votes.values() if t is not None)
    skips = sum(1 for t in game.votes.values() if t is None)
    head = "🗳 <b>Голосование завершено</b>" if final else (
        f"🗳 <b>Кто шпион?</b> На голосование {fmt_seconds(VOTING_SECONDS)}, голос можно менять.")
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


# ====== Ход игры ======

async def finish_game(game: Game, text: str, winner: str) -> None:
    if game.phase == "finished":
        return
    if game.phase == "voting":
        await safe_edit(game.chat_id, game.voting_message_id, voting_text(game, final=True))
    game.phase = "finished"
    cleanup(game)
    await send(game.chat_id,
               f"{text}\n\n🔑 Слово было: <b>{html.escape(game.word)}</b>\n\n🧾 Роли:\n{roles_summary(game)}")
    result = "победа шпионов 🕵️" if winner == SPY else "победа мирных 🕊️"
    for uid in game.all_players:
        verdict = "Ты выиграл! 🎉" if game.roles.get(uid) == winner else "Ты проиграл."
        await safe_dm(uid, f"🏁 Игра окончена: {result}. {verdict}")
    cancel_task(game)


async def stop_game(game: Game, text: str) -> None:
    if game.phase == "finished":
        return
    was_registration = game.phase in ("registration", "starting")
    if game.phase == "voting":
        await safe_edit(game.chat_id, game.voting_message_id, voting_text(game, final=True))
    game.phase = "finished"
    cleanup(game)
    cancel_task(game)
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


async def assign_roles(game: Game) -> None:
    ids = list(game.players)
    spy_count = max(1, len(ids) // 5)
    spies = set(random.sample(ids, spy_count))
    game.word = random.choice(WORDS)
    game.all_players = dict(game.players)
    game.roles = {uid: (SPY if uid in spies else CIVIL) for uid in ids}

    failed = [uid for uid in ids if not await safe_dm(uid, role_text(game, uid))]
    text = f"🎭 Роли розданы! Шпионов в игре: <b>{spy_count}</b>. Проверьте личку с ботом."
    if failed:
        text += ("\n\n❗ Не получили роль: " + ", ".join(mention(u, game.players[u]) for u in failed)
                 + ". Откройте бота и отправьте ему /role.")
    await send(game.chat_id, text)


async def discussion_phase(game: Game) -> None:
    game.phase = "discussion"
    game.skip_discussion.clear()
    names = "\n".join(f"• {mention(uid, name)}" for uid, name in game.players.items())
    await send(game.chat_id,
               f"🔁 <b>Раунд {game.round_no} из {MAX_ROUNDS}</b>\n👥 В игре ({len(game.players)}):\n{names}\n\n"
               f"🗣 Обсуждение — {fmt_seconds(DISCUSSION_SECONDS)}.\n"
               "Шпион может угадать слово: <code>/guess слово</code> в личку боту.\n"
               "Ведущий может сразу перейти к голосованию: /vote")
    warn_before = 30
    if DISCUSSION_SECONDS > warn_before * 2:
        if await wait_event(game.skip_discussion, DISCUSSION_SECONDS - warn_before):
            return
        await send(game.chat_id, f"⏳ Осталось {warn_before} секунд обсуждения!")
        await wait_event(game.skip_discussion, warn_before)
    else:
        await wait_event(game.skip_discussion, DISCUSSION_SECONDS)


async def voting_phase(game: Game) -> None:
    game.votes = {}
    game.all_voted.clear()
    game.phase = "voting"
    msg = await send(game.chat_id, voting_text(game), reply_markup=voting_markup(game))
    game.voting_message_id = msg.message_id if msg else None
    await wait_event(game.all_voted, VOTING_SECONDS)
    game.phase = "tally"


async def resolve_voting(game: Game) -> bool:
    """Подводит итог голосования. True — игра закончилась."""
    await safe_edit(game.chat_id, game.voting_message_id, voting_text(game, final=True))
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
    return await check_win(game)


async def run_game(game: Game) -> None:
    try:
        await wait_event(game.start_now, REGISTRATION_SECONDS)
        game.phase = "starting"
        if len(game.players) < MIN_PLAYERS:
            await stop_game(game, f"❗ Недостаточно игроков (нужно минимум {MIN_PLAYERS}). Набор закрыт.")
            return
        await safe_edit(game.chat_id, game.registration_message_id,
                        registration_text(game, closed="✅ Набор закрыт, игра началась!"))
        await assign_roles(game)

        while game.phase != "finished":
            game.round_no += 1
            await discussion_phase(game)
            if game.phase == "finished":
                return
            await voting_phase(game)
            if await resolve_voting(game):
                return
            if game.round_no >= MAX_ROUNDS:
                await finish_game(game, f"⌛ Шпионы продержались {MAX_ROUNDS} раундов. <b>Победа шпионов!</b>", SPY)
                return
    except asyncio.CancelledError:
        raise
    except Exception:
        log.exception("Ошибка в игре чата %s", game.chat_id)
        game.phase = "finished"
        cleanup(game)
        await send(game.chat_id, "⚠️ Внутренняя ошибка, игра остановлена. Начните заново: /game")


# ====== Команды ======

@router.message(CommandStart(), F.chat.type == ChatType.PRIVATE)
async def cmd_start_private(message: Message):
    await message.answer(
        "👋 Привет! Я веду игру «Шпион».\n\n"
        "Добавь меня в группу и напиши там /game.\n"
        "Если ты уже нажимал «Играть» — вернись в группу и нажми ещё раз, теперь я смогу прислать тебе роль.\n\n"
        "/rules — правила")


@router.message(Command("game", "start"), F.chat.type.in_(GROUP_TYPES))
async def cmd_game(message: Message):
    chat_id = message.chat.id
    if chat_id in games:
        await message.reply("Игра в этом чате уже идёт. Остановить: /end")
        return
    game = Game(chat_id=chat_id, host_id=message.from_user.id)
    games[chat_id] = game
    sent = await message.answer(registration_text(game), reply_markup=registration_markup())
    game.registration_message_id = sent.message_id
    game.task = asyncio.create_task(run_game(game))


@router.message(Command("game", "ready", "end", "vote"), F.chat.type == ChatType.PRIVATE)
async def cmd_group_only(message: Message):
    await message.answer("Эта команда работает в группе, где идёт игра.")


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
    game = games.get(player_game.get(message.from_user.id, 0))
    if not game or not game.roles or message.from_user.id not in game.roles:
        await message.answer("Ты сейчас не участвуешь в игре, или роли ещё не розданы.")
        return
    await message.answer(role_text(game, message.from_user.id))


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
    await check_win(game)


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
    global BOT_USERNAME
    me = await bot.get_me()
    BOT_USERNAME = me.username or ""
    await bot.set_my_commands([
        BotCommand(command="game", description="Начать набор в игру"),
        BotCommand(command="ready", description="Закончить набор и начать"),
        BotCommand(command="vote", description="Перейти к голосованию"),
        BotCommand(command="end", description="Остановить игру"),
        BotCommand(command="guess", description="Шпион: угадать слово (в личку)"),
        BotCommand(command="role", description="Напомнить мою роль (в личку)"),
        BotCommand(command="rules", description="Правила игры"),
    ])
    log.info("Бот @%s запущен", BOT_USERNAME)


async def main():
    global bot
    if not API_TOKEN:
        raise SystemExit("Не задана переменная окружения API_TOKEN")
    bot = Bot(token=API_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = Dispatcher()
    dp.include_router(router)
    dp.startup.register(on_startup)
    await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())


if __name__ == "__main__":
    asyncio.run(main())
