import asyncio
import datetime
import logging
import logging.handlers
import os
import re
import time

import aiogram
import humanize
from aiogram import types
from aiogram.contrib.fsm_storage.memory import MemoryStorage
from aiogram.dispatcher import FSMContext
from aiogram.dispatcher.filters.state import State, StatesGroup
from aiogram.types import inline_keyboard, reply_keyboard
from aiogram.utils import deep_linking

import config
import durak
import room

humanize.activate("ru_RU")

if not os.path.exists("./log"):
    os.mkdir("log")
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("bot")
fh = logging.handlers.TimedRotatingFileHandler("log/bot.log", encoding="utf-8", when="midnight")
formatter = logging.Formatter("[%(asctime)s] %(message)s")
fh.setFormatter(formatter)
logger.addHandler(fh)
logger.info("----- Бот запущен -----")

storage = MemoryStorage()
bot = aiogram.Bot("5767269718:AAEn1aJi-wAh2A7qhkOeDnDmaKg8eUNKIsM")

dp = aiogram.Dispatcher(bot, storage=storage)

queue_default: list[types.User] = []
queue_trans: list[types.User] = []
ongoing_games: list[durak.Game] = []
active_rooms: list[room.Room] = []

custom_room_keyboard = reply_keyboard.ReplyKeyboardMarkup()
custom_room_keyboard.add(reply_keyboard.KeyboardButton(
    "Задать дату и время начала *"))
custom_room_keyboard.add(
    reply_keyboard.KeyboardButton("Задать дату и время конца"))
custom_room_keyboard.add(reply_keyboard.KeyboardButton(
    "Задать максимальное количество игроков в комнате"))
custom_room_keyboard.add(reply_keyboard.KeyboardButton("Задать режим *"))
custom_room_keyboard.add(reply_keyboard.KeyboardButton(
    "Готово, получить пригласительную ссылку"))

DATETIME_REGEX = r"^([1-9]|(?:[012][0-9])|(?:3[01]))\.([0]{0,1}[1-9]|1[012])\.(\d\d\d\d) ([012]{0,1}[0-9]):([0-6][0-9])$"


class CustomGame(StatesGroup):
    start_time = State()
    end_time = State()
    max_players = State()
    gamemode = State()


@dp.message_handler(commands=['start', 'help', "menu"])
async def main_menu(message: types.Message):
    if payload := deep_linking.decode_payload(message.get_args()):
        for r in active_rooms:
            if r.unique_id == payload:
                if (await r.add_player_from_user(message.from_user)):
                    cancel_keyboard = reply_keyboard.ReplyKeyboardMarkup(resize_keyboard=True).add(
                        reply_keyboard.KeyboardButton("Выйти из комнаты"))
                    logger.info(f"{message.from_user.mention} ({message.from_user.id}) успешно заходит в комнату {payload}")
                    await message.reply("Вы были успешно добавлены в комнату.", reply_markup=cancel_keyboard)
                else:
                    logger.info(f"{message.from_user.mention} ({message.from_user.id}) не удаётся зайти в комнату {payload}")
                    await message.reply("Комната уже заполнена или вы в ней состоите!")
                break
        else:
            logger.info(f"{message.from_user.mention} ({message.from_user.id}) пытается зайти в несуществующую комнату {payload}")
            await message.reply("Нужная комната не была найдена. Удостоверьтесь, что вы не опоздали и получили правильную ссылку.")

    else:
        keyboard = inline_keyboard.InlineKeyboardMarkup(resize_keyboard=True)
        keyboard.add(inline_keyboard.InlineKeyboardButton(
            "Правила", callback_data="rules"))
        keyboard.add(inline_keyboard.InlineKeyboardButton(
            "FAQ", callback_data="faq"))
        keyboard.add(inline_keyboard.InlineKeyboardButton(
            "Связь с менеджером", url=config.MANAGER_USER_LINK))
        keyboard.add(inline_keyboard.InlineKeyboardButton(
            "Встать в очередь (подкидной)", callback_data="queue_default"))
        keyboard.add(inline_keyboard.InlineKeyboardButton(
            "Встать в очередь (переводной)", callback_data="queue_trans"))
        await message.reply("Добро пожаловать!\nВнимание: карты кроются в том порядке, в котором были выложены. Не спешите нажимать на кнопки. Они обновляются и вы можете случайно выложить неправильную карту. Вернуть её обратно в руку нельзя.", reply_markup=keyboard)


@dp.callback_query_handler()
async def handle_inlines(query: types.CallbackQuery):
    await query.answer(cache_time=1)

    if query.data == "queue_default":
        await check_and_process_queue(query.from_user, queue_default)
    elif query.data == "queue_trans":
        await check_and_process_queue(query.from_user, queue_trans)
    elif query.data == "rules":
        with open(config.RULES_FILE_PATH, "r", encoding="utf-8") as f:
            await query.message.answer(f.read(), parse_mode="markdown")
    elif query.data == "faq":
        with open(config.FAQ_FILE_PATH, "r", encoding="utf-8") as f:
            await query.message.answer(f.read(), parse_mode="markdown")


@dp.message_handler(commands=["create", "custom", "create_game", "create_room", "custom_game", "custom_room", "room", "private"], state="*")
async def create_custom_room(message: types.Message, state: FSMContext):
    if message.from_user.id in config.ADMIN_USER_IDS:
        logger.info(f"{message.from_user.mention} ({message.from_user.id}) использует команду /room")
        await message.reply("Пожалуйста, задайте следующие параметры, потом получите ссылку", reply_markup=custom_room_keyboard)


@dp.message_handler(commands=["delete", "delete_room", "cancel_room"])
async def delete_custom_room(message: types.Message):
    if message.from_user.id not in config.ADMIN_USER_IDS:
        return
    
    room_id = message.get_args()
    if not room_id:
        await message.reply("Укадите айди комнаты (пишется отдельным сообщением после создания комнаты).")
        return
    
    for r in active_rooms.copy():
        if r.unique_id == room_id:
            if r.started:
                await message.reply("Вы не можете удалить комнату, в которой уже идёт игра!")
            else:
                active_rooms.remove(r)
                for player in r.players:
                    await bot.send_message(player.user.id, "Комната, в которой вы состоите, была удалена.", reply_markup=reply_keyboard.ReplyKeyboardRemove())
                await message.reply("Комната была успешно удалена.")
                

@dp.message_handler(commands=["kick"])
async def room_kick(message: types.Message):
    if message.from_user.id not in config.ADMIN_USER_IDS:
        return

    player = message.get_args()
    if not player:
        await message.reply("Укажите айди (пишется при заходе в комнату) или @тег игрока.")
        return

    for r in active_rooms:
        for p in r.players:
            if (player.isdigit() and int(player) == p.user.id) or (player.startswith("@") and player == p.user.mention):
                await r.remove_player_from_user(p.user)
                logger.info(f"{message.from_user.mention} ({message.from_user.id}) кикает {player} из комнаты {r.unique_id}")
                await bot.send_message(p.user.id, "Администратор кикнул вас из комнаты.", reply_markup=reply_keyboard.ReplyKeyboardRemove())
                break
        else:
            await message.reply("Игрок не был найден ни в одной существующей комнате.")


@dp.message_handler(state=CustomGame.start_time)
async def handle_start_time(message: types.Message, state: FSMContext):
    if message.from_user.id not in config.ADMIN_USER_IDS:
        return
    if match := re.match(DATETIME_REGEX, message.text, flags=re.I):
        groups = match.groups()
        date = datetime.datetime(int(groups[2]), int(groups[1]), int(
            groups[0]), int(groups[3]), int(groups[4]))
        state_data = await state.get_data()
        if date <= datetime.datetime.now():
            await message.reply("Эта дата уже прошла. Укажите, пожалуйста, другую.")
            return
        if (end_time := state_data.get("end_time")):
            if date >= end_time:
                await message.reply("Дата начала должна быть раньше даты конца. Укажите, пожалуйста, другую.")
                return

        async with state.proxy() as data:
            data["start_time"] = date
        await state.set_state()
        await message.reply("Дата начала успешно установлена.", reply_markup=custom_room_keyboard)
    else:
        await message.reply("Укажите, пожалуйста, дату в правильном формате.")


@dp.message_handler(state=CustomGame.end_time)
async def handle_end_time(message: types.Message, state: FSMContext):
    if message.from_user.id not in config.ADMIN_USER_IDS:
        return
    if match := re.match(DATETIME_REGEX, message.text, flags=re.I):
        groups = match.groups()
        date = datetime.datetime(int(groups[2]), int(groups[1]), int(
            groups[0]), int(groups[3]), int(groups[4]))
        state_data = await state.get_data()
        if (start_time := state_data.get("start_time")):
            if start_time >= date:
                await message.reply("Дата конца должна быть позже даты начала. Укажите, пожалуйста, другую")
                return

        async with state.proxy() as data:
            data["end_time"] = date
        await state.set_state()
        await message.reply("Дата конца успешно установлена.", reply_markup=custom_room_keyboard)
    else:
        await message.reply("Укажите, пожалуйста, дату в правильном формате.")


@dp.message_handler(state=CustomGame.max_players)
async def handle_max_players(message: types.Message, state: FSMContext):
    def can_be_split(n: int) -> bool:
        def split_func(lst, sz): return [lst[i:i+sz]
                                         for i in range(0, len(lst), sz)]
        if n < 2:
            return []
        
        for players_per_game in range(6, 1, -1):
            if n % players_per_game == 0:
                result = split_func(range(n), players_per_game)
                return result if (can_be_split(len(result))) or len(result) == 1 else False
        return False

    if message.from_user.id not in config.ADMIN_USER_IDS:
        return
    if message.text.isdigit():
        async with state.proxy() as data:
            if int(message.text) % 2 != 0 and data.get("gamemode") in room.Gamemode.marathon():
                await message.reply("Число должно быть чётным!")
                return
            elif not can_be_split(int(message.text)) and data.get("gamemode") in room.Gamemode.tournament():
                await message.reply("Нельзя сыграть турнир таким количеством игроков!")
                return

            data["max_players"] = int(message.text)
            await state.set_state()
            await message.reply("Максимальное количество игроков успешно установлено", reply_markup=custom_room_keyboard)


@dp.message_handler(state=CustomGame.gamemode)
async def handle_gamemode(message: types.Message, state: FSMContext):
    if message.from_user.id not in config.ADMIN_USER_IDS:
        return
    if message.text.lower() == "марафон (подкидной дурак)":
        async with state.proxy() as data:
            data["gamemode"] = room.Gamemode.MARATHON_DEFAULT
    elif message.text.lower() == "марафон (переводной дурак)":
        async with state.proxy() as data:
            data["gamemode"] = room.Gamemode.MARATHON_TRANS
    elif message.text.lower() == "турнир (подкидной дурак)":
        async with state.proxy() as data:
            data["gamemode"] = room.Gamemode.TOURNAMENT_DEFAULT
    elif message.text.lower() == "турнир (переводной дурак)":
        async with state.proxy() as data:
            data["gamemode"] = room.Gamemode.TOURNAMENT_TRANS
    else:
        return
    await state.set_state()
    await message.reply("Режим успешно установлен", reply_markup=custom_room_keyboard)


@dp.message_handler(commands=["queue", "find_game"])
async def join_queue(message: types.Message):
    keyboard = inline_keyboard.InlineKeyboardMarkup()
    keyboard.add(inline_keyboard.InlineKeyboardButton(
        "Подкидной дурак", callback_data="queue_default"))
    keyboard.add(inline_keyboard.InlineKeyboardButton(
        "Переводной дурак", callback_data="queue_trans"))
    await message.reply("В какую игру вы хотите сыграть?", reply_markup=keyboard)


@dp.message_handler(commands=["queue_default", "find_game_default", "find_default"])
async def join_default_queue(message: types.Message):
    await check_and_process_queue(message.from_user, queue_default)


@dp.message_handler(commands=["queue_trans", "find_game_trans", "find_trans"])
async def join_trans_queue(message: types.Message):
    await check_and_process_queue(message.from_user, queue_trans)


async def check_and_process_queue(user: types.User, queue: list[types.User]):
    game_players = []
    for g in ongoing_games:
        game_players.extend([i.user for i in g.players])
    room_players = []
    for r in active_rooms:
        room_players.extend([i.user for i in r.players])
    if user in queue:
        await bot.send_message(user.id, "Вы уже состоите в очереди!")
    elif user in game_players:
        await bot.send_message(user.id, "Вы уже участвуете в игре!")
    elif user in room_players:
        await bot.send_message(user.id, "Вы сейчас находитесь в комнате!")
    else:
        keyboard = reply_keyboard.ReplyKeyboardMarkup(resize_keyboard=True)
        keyboard.add(reply_keyboard.KeyboardButton("Отменить"))
        queue.append(user)
        logger.info(f"{user.mention} ({user.id}) присоединился к очереди в {'подкидного' if queue is queue_default else 'переводного'} дурака. Длина очереди: {len(queue)}")
        await bot.send_message(user.id, "Вы были успешно добавлены в очередь! Вы будете оповещены, когда начнётся игра.", reply_markup=keyboard)

    if len(queue) >= 2:
        game = durak.Game([durak.Player(p1 := queue.pop()), durak.Player(
            p2 := queue.pop())], bot=bot, move_time=config.SECONDS_FOR_MOVE, is_transferrable=queue is queue_trans)
        ongoing_games.append(game)
        # to ensure that users leave both queues
        for p in (p1, p2):
            for q in (queue_default, queue_trans):
                if p in q:
                    q.remove(p)
                    break
        await game.game_loop()
        ongoing_games.remove(game)


@dp.message_handler(state="*")
async def message_handler(message: types.Message, state: FSMContext):
    await queue_cancel_handler(message)
    await game_handler(message)
    await custom_room_handler(message, state)
    await room_leave_handler(message)


async def room_leave_handler(message: types.Message):
    if message.text.lower() == "выйти из комнаты":
        for r in active_rooms:
            for player in r.players:
                if player.user == message.from_user:
                    if not r.started:
                        await r.remove_player_from_user(message.from_user)
                        logger.info(f"{message.from_user.mention} ({message.from_user.id}) вышел из комнаты {r.unique_id}")
                        await message.reply("Вы успешно вышли из комнаты", reply_markup=reply_keyboard.ReplyKeyboardRemove())
                    else:
                        await message.reply("Вы не можете выйти из комнаты после начала события!")
                    break


async def queue_cancel_handler(message: types.Message):
    if message.text.lower() == "отменить":
        if message.from_user in queue_default:
            queue_default.remove(message.from_user)
            logger.info(f"{message.from_user.mention} ({message.from_user.id}) вышел из очереди в подкидного дурака")
            await message.reply("Вы были успешно исключены из очереди в подкидного дурака!", reply_markup=reply_keyboard.ReplyKeyboardRemove())
        if message.from_user in queue_trans:
            queue_trans.remove(message.from_user)
            logger.info(f"{message.from_user.mention} ({message.from_user.id}) вышел из очереди в переводного дурака")
            await message.reply("Вы были успешно исключены из очереди в переводного дурака!", reply_markup=reply_keyboard.ReplyKeyboardRemove())


async def game_handler(message: types.Message):
    user = message.from_user
    games = ongoing_games.copy()
    for r in active_rooms:
        games.extend(r.games)
    for g in games:
        if user in [i.user for i in g.players] and g.current_player.user == user and g.running:
            await g.move_handler(message)
            break


async def custom_room_handler(message: types.Message, state: FSMContext):
    if message.from_user.id not in config.ADMIN_USER_IDS:
        return
    if message.text.lower() == "задать дату и время начала *":
        await CustomGame.start_time.set()
        await message.reply("Напишите дату и время начала в формате дд.мм.гггг чч:мм\n" + f"Текущее время на компьютере бота: {room.format_time(time.localtime())}")
    elif message.text.lower() == "задать дату и время конца":
        await CustomGame.end_time.set()
        await message.reply("Напишите дату и время конца в формате дд.мм.гггг чч:мм\n" + f"Текущее время на компьютере бота: {room.format_time(time.localtime())}")
    elif message.text.lower() == "задать максимальное количество игроков в комнате":
        await CustomGame.max_players.set()
        if (await state.get_data()).get("gamemode") in room.Gamemode.marathon():
            await message.reply("Напишите желаемое максимальное количество игроков (должно быть чётным для марафона):")
        else:
            await message.reply("Напишите желаемое максимальное количество игроков (не между любым количеством людей можно сыграть турнир):")
    elif message.text.lower() == "готово, получить пригласительную ссылку":
        data = await state.get_data()
        if (all([data.get("start_time"), data.get("gamemode")])):
            print(data)
            new_room = room.Room(data["start_time"], data.get("end_time"), data.get(
                "max_players"), data.get("gamemode"), admin=message.from_user, bot=bot)
            active_rooms.append(new_room)
            
            start_time = room.format_time(time.localtime(data.get('start_time').timestamp()))
            end_time = room.format_time(time.localtime(data.get('end_time').timestamp())) if data.get('end_time') else 'нет'
            gamemode = str(data['gamemode'])
            max_players = data.get('max_players') or 'не ограничено'
            event_eta = humanize.precisedelta(datetime.datetime.now() - data.get('start_time'), minimum_unit='minutes')
            invite_link = await new_room.invite_link
            
            logger.info(f"{message.from_user.mention} ({message.from_user.id}) создал новую комнату: {start_time} - {end_time}, {gamemode}, до {max_players} чел.; через: {event_eta};   {invite_link}")
            
            await message.reply(f"Вы успешно создали комнату!\nДата начала: {start_time}\nДата конца: {end_time}\nРежим: {gamemode}\nМаксимальное количество игроков: {max_players}\nСобытие начнётся через: {event_eta}\n\nВсе игроки должны перейти по следующей ссылке: {invite_link}\nОднако администратору, создавшему комнату, всё равно будут приходить некоторые уведомления о ходе события.", reply_markup=reply_keyboard.ReplyKeyboardRemove())
            await message.reply(f"Айди для удаления комнаты: {new_room.unique_id}")

            await state.finish()

        else:
            print(data)
            await message.reply("Укажите все данные, помеченные звёздочкой!")
    elif message.text.lower() == "задать максимальное количество игроков в комнате *":
        await CustomGame.max_players.set()
        await message.reply("Напишите максимальное количество игроков в комнате")
    elif message.text.lower() == "задать режим *":
        gamemode_keyboard = reply_keyboard.ReplyKeyboardMarkup()
        gamemode_keyboard.add(reply_keyboard.KeyboardButton(
            "Марафон (подкидной дурак)"))
        gamemode_keyboard.add(reply_keyboard.KeyboardButton(
            "Марафон (переводной дурак)"))
        gamemode_keyboard.add(
            reply_keyboard.KeyboardButton("Турнир (подкидной дурак)"))
        gamemode_keyboard.add(reply_keyboard.KeyboardButton(
            "Турнир (переводной дурак)"))
        await CustomGame.gamemode.set()
        await message.reply("Выберите режим игры", reply_markup=gamemode_keyboard)


async def check_rooms():
    while True:
        for r in active_rooms.copy():
            if not r.started and datetime.datetime.now() >= r.start_time:
                await r.start()
                if r.started:
                    print("starting a room")
                    asyncio.create_task(r.loop())
            elif (r.started and not r.running):
                await r.end()
                active_rooms.remove(r)
            elif r.end_time:
                if datetime.datetime.now() >= r.end_time:
                    await r.end()
                    active_rooms.remove(r)
        await asyncio.sleep(30)


async def on_startup(_dp):
    asyncio.create_task(check_rooms())
aiogram.executor.start_polling(dp, skip_updates=True, on_startup=on_startup)
