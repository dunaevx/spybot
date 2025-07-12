import random
import asyncio
import os
from aiogram import Bot, Dispatcher, executor, types
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton, BotCommand
from collections import defaultdict

API_TOKEN = '7944223394:AAFJTGHXQ69W3biGnFRZj96vcbev2hsKoaI'
bot = Bot(token=API_TOKEN)
dp = Dispatcher(bot)

game_data = {
    'in_progress': False,
    'registration_open': False,
    'players': set(),
    'roles': {},
    'word': '',
    'spy_count': 1,
    'votes': defaultdict(int),
    'voted_players': set(),
    'voting_active': False,
    'voting_message_id': None,
    'player_names': {},
    'chat_id': None
}

registration_message = None
WORDS = [
    'библиотека', 'пиццерия', 'пляж', 'аэропорт', 'школа', 'больница', 'кафе', 'поезд', 'кинотеатр',
    'университет', 'парк', 'музей', 'зоопарк', 'магазин', 'метро', 'автобус', 'ресторан', 'банк',
    'офис', 'спортзал', 'тюрьма', 'театр', 'церковь', 'стадион', 'космический корабль', 'ферма',
    'гостиница', 'ночной клуб', 'свадьба', 'похороны', 'пожарная станция', 'военная база',
    'детский сад', 'гараж', 'квартира', 'лагерь', 'сауна', 'рыбалка', 'кабинет врача', 'пекарня',
    'космодром', 'пароход', 'порт', 'рынок', 'концерт', 'супермаркет', 'автостоянка', 'гору',
    'граница', 'пустыня', 'лес', 'вулкан', 'завод', 'шахта', 'конференция', 'пейнтбол', 'боулинг',
    'тренинг', 'пляжный бар', 'столовая', 'бассейн', 'клуб настольных игр', 'туристическая база',
    'участок полиции', 'космическая станция', 'крейсер', 'футбольное поле', 'гольф-клуб',
    'массажный салон', 'прачечная', 'казино', 'бункер', 'телестудия', 'радиостанция', 'суд',
    'лаборатория', 'кухня', 'цирк', 'танцпол', 'дискотека', 'киностудия', 'зоомагазин', 'шиномонтаж',
    'барбершоп', 'парикмахерская', 'винодельня', 'ремонтная мастерская', 'ателье', 'галерея',
    'аквапарк', 'игровой зал', 'заправка', 'чемпионат', 'метеостанция', 'автошкола', 'вертолётная площадка',
    'свалка', 'лифт', 'чердак', 'подвал', 'водоочистная станция', 'зоопитомник', 'обсерватория',
    'рекламное агентство', 'кафедра университета', 'читальный зал', 'садоводческое товарищество'
]

RULES_TEXT = """
🎲 **Правила игры "Шпион"**

1. В игре участвуют мирные жители и шпионы.
2. Мирные знают секретное слово и должны его защищать.
3. Шпионы не знают секретное слово и должны угадать его, прослушивая обсуждения.
4. Игра проходит в раундах:
   - Обсуждение (2 минуты): игроки общаются в чате, пытаясь вычислить шпионов.
   - Голосование: все голосуют за подозреваемого шпиона.
5. Если все шпионы устранены — побеждают мирные.
6. Если шпионы угадывают слово или остаются в большинстве — побеждают шпионы.
7. Если в игре остаётся всего 2 игрока, и среди них есть шпион — шпион выигрывает автоматически.
Удачи!
"""

def reset_game_data():
    game_data.update({
        'in_progress': False,
        'registration_open': False,
        'players': set(),
        'roles': {},
        'word': '',
        'spy_count': 1,
        'votes': defaultdict(int),
        'voted_players': set(),
        'voting_active': False,
        'voting_message_id': None,
        'player_names': {},
        'chat_id': None
    })

async def on_startup(dispatcher):
    commands = [
        BotCommand(command="start", description="Начать игру"),
        BotCommand(command="ready", description="Завершить регистрацию и начать игру"),
        BotCommand(command="end", description="Завершить игру досрочно"),
        BotCommand(command="rules", description="Показать правила игры"),
        BotCommand(command="chatid", description="Показать ID текущего чата"),
    ]
    await bot.set_my_commands(commands)

@dp.message_handler(commands=['start'])
async def start_game(message: types.Message):
    if game_data['in_progress']:
        await message.reply("Игра уже идёт!")
        return

    game_data.update({
        'chat_id': message.chat.id,
        'players': set(),
        'roles': {},
        'votes': defaultdict(int),
        'voted_players': set(),
        'voting_active': False,
        'voting_message_id': None,
        'player_names': {},
        'in_progress': True,
        'registration_open': True
    })

    keyboard = InlineKeyboardMarkup().add(InlineKeyboardButton("Играть", callback_data="join_game"))

    global registration_message
    registration_message = await message.answer(
        "🎮 Игра начинается! Нажмите 'Играть', чтобы зарегистрироваться.\n\n👥 Зарегистрированы: никто",
        reply_markup=keyboard
    )

@dp.message_handler(commands=['ready'])
async def ready_start(message: types.Message):
    if game_data['in_progress'] and game_data['registration_open']:
        game_data['registration_open'] = False
        await message.answer("✅ Регистрация завершена вручную. Начинаем игру!")
        await assign_roles()
    else:
        await message.answer("❌ Сейчас нельзя завершить регистрацию.")

@dp.message_handler(commands=['end'])
async def end_game(message: types.Message):
    global registration_message
    if game_data['in_progress']:
        for pid in game_data['players']:
            try:
                await bot.send_message(pid, "🛑 Игра досрочно завершена.")
            except:
                pass
        reset_game_data()
        registration_message = None
        await message.answer("🛑 Игра досрочно завершена.")
    else:
        await message.answer("❌ Сейчас нет активной игры.")

@dp.message_handler(commands=['rules'])
async def send_rules(message: types.Message):
    await message.answer(RULES_TEXT, parse_mode='Markdown')

@dp.callback_query_handler(lambda c: c.data == 'join_game')
async def join_game(callback_query: types.CallbackQuery):
    user = callback_query.from_user
    user_id = user.id

    if user_id in game_data['players']:
        await callback_query.answer("Вы уже зарегистрированы.")
        return

    try:
        await bot.send_message(user_id, "✅ Вы зарегистрированы в игре!")
    except Exception:
        await bot.send_message(
            game_data['chat_id'],
            f"❗ <a href='tg://user?id={user_id}'>{user.full_name}</a> не запустил бота. "
            f"Чтобы участвовать, нужно сначала написать боту в личные сообщения.",
            parse_mode='HTML'
        )
        await callback_query.answer("Сначала напишите боту в личные сообщения!")
        return

    game_data['players'].add(user_id)
    await callback_query.answer("Вы зарегистрированы!")

    global registration_message
    if registration_message:
        text = "🎮 Игра начинается! Нажмите 'Играть', чтобы зарегистрироваться.\n\n"
        text += "👥 Зарегистрированы:\n" + "\n".join(
            [f"• <a href='tg://user?id={pid}'>{(await bot.get_chat_member(game_data['chat_id'], pid)).user.full_name}</a>"
             for pid in game_data['players']]
        )
        try:
            await bot.edit_message_text(
                text,
                chat_id=game_data['chat_id'],
                message_id=registration_message.message_id,
                reply_markup=callback_query.message.reply_markup,
                parse_mode='HTML'
            )
        except:
            pass

async def assign_roles():
    global registration_message
    players = list(game_data['players'])
    if len(players) < 3:
        await bot.send_message(game_data['chat_id'], "❗ Недостаточно игроков (нужно минимум 3).")
        reset_game_data()
        return

    registration_message = None
    game_data['word'] = random.choice(WORDS)
    game_data['spy_count'] = max(1, len(players) // 5)
    spies = set(random.sample(players, game_data['spy_count']))

    for pid in players:
        role = "шпион" if pid in spies else "мирный"
        game_data['roles'][pid] = role
        msg = (
            "🔎 Вы шпион! Попробуйте угадать слово в группе (просто напишите его)."
            if role == 'шпион' else f"🕊️ Вы мирный. Слово: {game_data['word']}"
        )
        try:
            await bot.send_message(pid, msg)
        except:
            await bot.send_message(game_data['chat_id'], f"❗ Не удалось отправить сообщение игроку: {pid}")

    await start_round()

async def start_round():
    players = list(game_data['players'])
    names = []
    for pid in players:
        try:
            user = await bot.get_chat_member(game_data['chat_id'], pid)
            names.append(f"<a href='tg://user?id={pid}'>{user.user.full_name}</a>")
        except:
            names.append(str(pid))

    await bot.send_message(game_data['chat_id'], "👥 Игроки в игре:\n" + "\n".join(f"• {name}" for name in names), parse_mode='HTML')
    await bot.send_message(game_data['chat_id'], "🗣️ Обсуждение началось. У вас есть 2 минуты!")
    await asyncio.sleep(120)  # 2 minutes for discussion
    await bot.send_message(game_data['chat_id'], "⏰ Время обсуждения закончилось! Переходим к голосованию.")
    await start_voting()

async def start_voting():
    game_data['voted_players'] = set()
    game_data['voting_active'] = True
    game_data['votes'] = defaultdict(int)

    player_names = {}
    for pid in game_data['players']:
        try:
            user = await bot.get_chat_member(game_data['chat_id'], pid)
            player_names[pid] = user.user.full_name
        except:
            player_names[pid] = str(pid)
    game_data['player_names'] = player_names

    markup = InlineKeyboardMarkup(row_width=2)
    for pid in game_data['players']:
        markup.add(InlineKeyboardButton(player_names[pid], callback_data=f"vote_{pid}"))
    markup.add(InlineKeyboardButton("Пропустить", callback_data="Пропустить"))

    text = "🗳️ Голосуем за шпиона:\n"
    for pid in game_data['players']:
        name = player_names[pid]
        votes = game_data['votes'][pid]
        text += f"• {name} ({votes})\n"
    text += "\nЕщё не проголосовали:\n"
    for pid in game_data['players']:
        name = player_names[pid]
        text += f"• {name}\n"

    voting_message = await bot.send_message(game_data['chat_id'], text, reply_markup=markup, parse_mode='HTML')
    game_data['voting_message_id'] = voting_message.message_id

    async def voting_timer():
        start_time = asyncio.get_event_loop().time()
        while asyncio.get_event_loop().time() - start_time < 60:
            if len(game_data['voted_players']) >= len(game_data['players']):
                break
            await asyncio.sleep(1)
        game_data['voting_active'] = False
        await finish_voting()

    asyncio.create_task(voting_timer())

@dp.callback_query_handler(lambda c: c.data.startswith("vote_"))
async def handle_vote(callback_query: types.CallbackQuery):
    if not game_data['voting_active']:
        await callback_query.answer("Голосование уже завершено.")
        return
    voter = callback_query.from_user.id
    if voter not in game_data['players']:
        await callback_query.answer("Вы не зарегистрированы в игре.")
        return
    if voter in game_data['voted_players']:
        await callback_query.answer("Вы уже голосовали.")
        return
    target = int(callback_query.data.split("_")[1])
    game_data['votes'][target] += 1
    game_data['voted_players'].add(voter)
    await callback_query.answer("Голос принят.")
    await update_voting_message()

@dp.callback_query_handler(lambda c: c.data == "Пропустить")
async def handle_skip_vote(callback_query: types.CallbackQuery):
    if not game_data['voting_active']:
        await callback_query.answer("Голосование уже завершено.")
        return
    voter = callback_query.from_user.id
    if voter not in game_data['players']:
        await callback_query.answer("Вы не зарегистрированы в игре.")
        return
    if voter in game_data['voted_players']:
        await callback_query.answer("Вы уже пропустили голосование.")
        return
    game_data['voted_players'].add(voter)
    await callback_query.answer("Вы пропустили голосование.")
    await update_voting_message()

async def update_voting_message():
    text = "🗳️ Голосуем за шпиона:\n"
    for pid in game_data['players']:
        name = game_data['player_names'][pid]
        votes = game_data['votes'][pid]
        text += f"• {name} ({votes})\n"
    text += "\nЕщё не проголосовали:\n"
    not_voted = game_data['players'] - game_data['voted_players']
    if not_voted:
        for pid in not_voted:
            name = game_data['player_names'][pid]
            text += f"• {name}\n"
    else:
        text += "Все игроки проголосовали.\n"
    markup = InlineKeyboardMarkup(row_width=2)
    for pid in game_data['players']:
        name = game_data['player_names'][pid]
        markup.add(InlineKeyboardButton(name, callback_data=f"vote_{pid}"))
    markup.add(InlineKeyboardButton("Skip vote", callback_data="skip_vote"))
    try:
        await bot.edit_message_text(
            text,
            chat_id=game_data['chat_id'],
            message_id=game_data['voting_message_id'],
            reply_markup=markup,
            parse_mode='HTML'
        )
    except:
        pass

async def finish_voting():
    game_data['voting_active'] = False
    text = "🗳️ Голосование завершено.\n"
    for pid in game_data['players']:
        name = game_data['player_names'][pid]
        votes = game_data['votes'][pid]
        text += f"• {name} ({votes})\n"
    await bot.edit_message_text(
        text,
        chat_id=game_data['chat_id'],
        message_id=game_data['voting_message_id'],
        parse_mode='HTML'
    )

    max_votes = max(game_data['votes'].values()) if game_data['votes'] else 0
    candidates = [pid for pid, count in game_data['votes'].items() if count == max_votes]
    if len(candidates) == 1:
        eliminated = candidates[0]
        eliminated_name = game_data['player_names'][eliminated]
        await bot.send_message(game_data['chat_id'], f"❌ Выбывает: {eliminated_name}", parse_mode='HTML')
        remaining = game_data['players'] - {eliminated}
    else:
        await bot.send_message(game_data['chat_id'], "🤝 Ничья в голосовании. Никто не выбывает.")
        remaining = game_data['players']

    spies = [pid for pid in remaining if game_data['roles'][pid] == 'шпион']
    peaceful = [pid for pid in remaining if game_data['roles'][pid] == 'мирный']

    if len(remaining) == 2 and len(spies) >= 1:
        await bot.send_message(game_data['chat_id'], "🕵️ В игре осталось всего 2 игрока, и среди них есть шпион. Победа шпиона!")
        for pid in remaining:
            try:
                await bot.send_message(pid, "Игра окончена: Победа шпиона!")
            except:
                pass
        await reveal_roles()
        reset_game_data()
    elif not spies:
        await bot.send_message(game_data['chat_id'], "🎉 Победа мирных!")
        for pid in remaining:
            try:
                await bot.send_message(pid, "Игра окончена: Победа мирных!")
            except:
                pass
        await reveal_roles()
        reset_game_data()
    elif not peaceful:
        await bot.send_message(game_data['chat_id'], f"🕵️ Победа шпионов! Слово было: {game_data['word']}")
        for pid in remaining:
            try:
                await bot.send_message(pid, "Игра окончена: Победа шпионов!")
            except:
                pass
        await reveal_roles()
        reset_game_data()
    else:
        game_data['players'] = remaining
        game_data['votes'].clear()
        game_data['voted_players'].clear()
        await bot.send_message(game_data['chat_id'], "🔁 Следующий раунд...")
        await start_round()

async def reveal_roles():
    role_list = []
    for pid, role in game_data['roles'].items():
        try:
            user = await bot.get_chat_member(game_data['chat_id'], pid)
            name = user.user.full_name
        except:
            name = str(pid)
        role_list.append(f"• <a href='tg://user?id={pid}'>{name}</a> — {role}")
    await bot.send_message(
        game_data['chat_id'],
        "🧾 Роли игроков:\n" + "\n".join(role_list),
        parse_mode='HTML'
    )

@dp.message_handler(lambda message: game_data['in_progress'] and message.chat.id == game_data['chat_id'])
async def guess_by_spy(message: types.Message):
    user_id = message.from_user.id
    role = game_data['roles'].get(user_id)
    guessed = message.text.strip().lower()
    target = game_data['word'].strip().lower()

    if role == 'шпион' and guessed == target:
        await bot.send_message(game_data['chat_id'],
                               f"🎯 Шпион <a href='tg://user?id={user_id}'>{message.from_user.full_name}</a> угадал слово: {game_data['word']}",
                               parse_mode='HTML')
        await bot.send_message(game_data['chat_id'], "🕵️ Победа шпионов!")
        for pid in game_data['players']:
            try:
                await bot.send_message(pid, "Игра окончена: Победа шпионов!")
            except:
                pass
        await reveal_roles()
        reset_game_data()

@dp.message_handler(commands=['chatid'])
async def show_chat_id(message: types.Message):
    await message.reply(f"ID этого чата: {message.chat.id}, тип: {message.chat.type}")

if __name__ == '__main__':
    executor.start_polling(dp, skip_updates=True, on_startup=on_startup)