import asyncio
import contextlib
import enum
import random
import logging
import time
import uuid
from datetime import datetime, timedelta

import humanize
from aiogram import Bot, exceptions, types
from aiogram.utils import deep_linking

import config
import durak

logger = logging.getLogger("bot")

def format_time(unix_time: time.struct_time):
    return time.strftime("%d.%m.%Y %H:%M", unix_time)


class Gamemode(enum.IntEnum):
    MARATHON_DEFAULT = 1
    MARATHON_TRANS = 2
    TOURNAMENT_DEFAULT = 3
    TOURNAMENT_TRANS = 4

    @staticmethod
    def marathon() -> list["Gamemode"]:
        return [Gamemode.MARATHON_DEFAULT, Gamemode.MARATHON_TRANS]

    @staticmethod
    def tournament() -> list["Gamemode"]:
        return [Gamemode.TOURNAMENT_DEFAULT, Gamemode.TOURNAMENT_TRANS]

    def __str__(self):
        match self:
            case Gamemode.MARATHON_DEFAULT:
                return "Марафон (подкидной дурак)"
            case Gamemode.MARATHON_TRANS:
                return "Марафон (переводной дурак)"
            case Gamemode.TOURNAMENT_DEFAULT:
                return "Турнир (подкидной дурак)"
            case Gamemode.TOURNAMENT_TRANS:
                return "Турнир (переводной дурак)"
        return "Неизвестный режим"  # линтер, не ругайся


class Room:
    def __init__(self, start_time: datetime, end_time: datetime, max_players: int, gamemode: Gamemode, admin: types.User, bot):
        self.admin = admin
        self.bot: Bot = bot

        self.running = False
        self.started = False

        self.start_time = start_time
        self.end_time = end_time

        self.max_players = max_players
        self.gamemode = gamemode

        self.games: list[durak.Game] = []
        self.marathon_queue: list[durak.Player] = []

        self.scores: list[int] = []
        self.unique_id: uuid.UUID = str(uuid.uuid4())
        self.players: list[durak.Player] = []

    @property
    async def invite_link(self) -> str:
        return await deep_linking.get_start_link(self.unique_id, encode=True)

    @property
    def everyone(self) -> set[durak.Player]:
        """ Returns every player in the room and the admin who created it"""
        return set([self.admin] + [i.user for i in self.players])

    @property
    def players_only(self) -> set[durak.Player]:
        """Returns every player in the room except the admin"""
        return {i.user for i in self.players} - {self.admin}

    async def send_message(self, target: list[types.User], text: str, notifications: bool = True) -> None:
        for u in target:
            try:
                await self.bot.send_message(u.id, text, disable_notification=not notifications)
            except (exceptions.BotBlocked, exceptions.UserDeactivated, exceptions.ChatNotFound):
                logger.info(f"{u.mention} ({u.id}) недоступен для бота, кикнут из комнаты {self.unique_id}")
                await self.remove_player_from_user(u)
                await self.send_message(self.everyone, f"{u.mention} заблокировал бота, или же бот по другим причинам не смог с ним связаться, поэтому пользователь вылетает из игры.")
            except exceptions.RetryAfter as exc:
                await asyncio.sleep(exc.timeout)
                # Recursive call
                await self.send_message([u], text, notifications=notifications)

    async def add_player_from_user(self, user: types.User) -> bool:
        # check if does not break
        if self.max_players and len(self.players) >= self.max_players:
            return False
        if user in [p.user for p in self.players]:
            await self.send_message([user], "Вы уже присоединились к этой комнате!")
            return False

        self.players.append(durak.Player(user))
        await self.send_message(self.players_only, f"{user.mention} добавился в комнату. Сейчас в комнате {len(self.players)} человек(а).")
        await self.send_message([self.admin], f"{user.mention} добавился в комнату. Сейчас в комнате {len(self.players)} человек(а).\nID: {user.id}")
        return True

    async def remove_player_from_user(self, user: types.User) -> None:
        for player in self.players:
            if player.user == user:
                index = self.players.index(player)
                self.players.pop(index)
                with contextlib.suppress(IndexError):
                    self.scores.pop(index)
                with contextlib.suppress(ValueError):
                    self.marathon_queue.remove(player)
        await self.send_message(self.everyone, f"{user.mention} выходит из комнаты (осталось {len(self.players)} человек)")

    async def reschedule(self, delta: timedelta):
        if self.started:
            return
        self.start_time += delta
        if self.end_time:
            self.end_time += delta
        
        event_eta = humanize.precisedelta(datetime.now() - self.start_time)
        logger.info(f"Начало события в комнате {self.unique_id} перенесены на {event_eta}")
        await self.send_message(self.everyone, f"Начало и конец события были перенесены. Игра начнётся через: {event_eta}")

    async def split_tournament_players(self, players: list[durak.Player]) -> list[list[durak.Game]]:
        def split_func(lst, sz): return [lst[i:i+sz]
                                         for i in range(0, len(lst), sz)]
        if not players:
            return []

        for players_per_game in range(6, 1, -1):
            if len(players) % players_per_game == 0:
                result = split_func(players, players_per_game)
                print(len(players), "%", players_per_game)
                return result if (await self.split_tournament_players(result)) != [] or len(result) == 1 else []
        print(len(players), "% none")
        return []

    async def marathon_gameloop(self, player1: durak.Player, player2: durak.Player) -> None:
        game = durak.Game([player1, player2], bot=self.bot, move_time=config.SECONDS_FOR_MOVE,
                          is_transferrable=(self.gamemode == Gamemode.MARATHON_TRANS))
        self.games.append(game)
        logger.info(f"[{game.unique_id}] Начало раунда марафона в комнате {self.unique_id}")
        winner = await game.game_loop()
        if winner != "draw":
            self.scores[self.players.index(winner)] += 1
        player1.previously_played_with = player2
        player2.previously_played_with = player1
        self.marathon_queue.extend([player1, player2])
        self.games.remove(game)

        results = '\n'.join(['{0} : {1}'.format(i.user.mention, j)
                            for i, j in zip(self.players, self.scores)])
        await self.send_message(self.everyone, f"Текущая таблица результатов:\n{results}")

    async def process_marathon_winner(self) -> None:
        if self.gamemode in Gamemode.marathon():
            if self.scores.count(max(self.scores)) >= 2:
                # find N max score indexes, then players
                print(self.scores, self.players)
                winners = [self.players[index] for index, score in enumerate(
                    self.scores) if score == max(self.scores)]
                winners_str = ', '.join(i.user.mention for i in winners)
                
                logger.info(f"Марафон в комнате {self.unique_id} закончился вничью между {winners_str}")
                await self.send_message(self.everyone, f"По итогам сыгранных раундов, игра закончилась вничью между {winners_str}!")

            else:
                winner = self.players[self.scores.index(max(self.scores))]
                logger.info(f"Марафон в комнате {self.unique_id} закончился победой {winner.user.mention} ({winner.user.id})")
                await self.send_message(self.everyone, f"Итоговый победитель марафона по результатам сыгранных раундов: {winner.user.mention}!")
                
            results = '; '.join(['{0} : {1}'.format(i.user.mention, j)
                            for i, j in zip(self.players, self.scores)])
            logger.info(f"{self.unique_id}: таблица результатов - {results}")

    async def start(self) -> None:
        if ((len(self.players) < 2 or len(self.players) % 2 != 0) and (self.gamemode in Gamemode.marathon())) or (not (await self.split_tournament_players(self.players)) and self.gamemode in Gamemode.tournament()):
            await self.send_message(self.everyone, "Игра должна была начаться, но количество игроков в комнате не соответствует требованиям.")
            await self.reschedule(timedelta(minutes=2))
            return

        random.shuffle(self.players)
        self.scores = [0 for _ in range(len(self.players))]
        self.started = True

    async def marathon_loop(self) -> None:
        if len(self.marathon_queue) >= 2:
            for p in self.marathon_queue.copy():
                pair_found = False
                for p2 in self.marathon_queue.copy():
                    if p2 is not p and (p.previously_played_with != p2 or len(self.players) == 2):
                        asyncio.create_task(self.marathon_gameloop(p, p2))
                        self.marathon_queue.remove(p)
                        self.marathon_queue.remove(p2)
                        pair_found = True
                        break
                    if pair_found:
                        break
        if self.running:
            await asyncio.sleep(2)
            await self.marathon_loop()

    async def wait_for_games_to_end(self) -> list[durak.Player]:
        async def tournament_loop(game: durak.Game):
            winner = "draw"
            while winner == "draw":
                winner = await game.game_loop()
                if winner == "draw":
                    logger.info(f"[{game.unique_id}] Игра закончилась вничью, игроки переигрывают.")
                    await self.send_message(list({self.admin, *game.players}), " vs ".join(i.user.mention for i in game.players) + "\n\nИгра закончилась вничью. Игроки переигрывают.")
            return winner
        async with asyncio.TaskGroup() as tg:
            tasks = [tg.create_task(tournament_loop(g)) for g in self.games]
        return [t.result() for t in tasks]

    async def loop(self) -> None:
        self.running = True
        if self.gamemode in Gamemode.tournament():
            winner = None
            players = self.players.copy()
            while not winner:
                self.games = [durak.Game([durak.Player(i.user) for i in player_group], bot=self.bot, move_time=config.SECONDS_FOR_MOVE,
                                         is_transferrable=self.gamemode == Gamemode.TOURNAMENT_TRANS) for player_group in (await self.split_tournament_players(players))]
                
                games_message_text = "Текущие столы:\n\n"
                for index, player_group in enumerate(await self.split_tournament_players(players)):
                    games_message_text += f"Стол {index + 1}: " + ", ".join(i.user.mention for i in player_group) + "\n"
                await self.send_message([self.admin], games_message_text)

                players = await self.wait_for_games_to_end()
                if len(players) == 1:
                    winner = players[0]

            if self.running:
                if winner != "draw":
                    logger.info(f"Турнир в комнате {self.unique_id} закончился победой {winner.user.mention} ({winner.user.id})")
                    await self.send_message(self.everyone, f"Поздравляем! Победитель турнира - {winner.user.mention}!")
                else:
                    logger.info(f"Турнир в комнате {self.unique_id} закончился вничью")
                    await self.send_message(self.everyone, "Поздравляем! Турнир закончился вничью!")
            self.games = []
        else:
            self.marathon_queue = self.players.copy()
            await self.marathon_loop()

    async def end(self) -> None:
        self.running = False
        # different game endings due to timeout here
        await self.process_marathon_winner()
        if self.gamemode in Gamemode.tournament():
            logger.info(f"Турнир в комнате {self.unique_id} закончился таймаутом")
            await self.send_message(self.everyone, "Время на турнир вышло!")
        for game in self.games:
            game.running = False
        self.games = []
