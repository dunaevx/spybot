"""Хранилище: Postgres (если задан DATABASE_URL) или SQLite-файл.

Хранит настройки чатов, свои слова, статистику игроков и снимки идущих игр,
чтобы после перезапуска бота партия продолжалась.
"""
import asyncio
import json
import logging
import re
import sqlite3

log = logging.getLogger("spybot.storage")

SCHEMA = [
    "CREATE TABLE IF NOT EXISTS chat_settings (chat_id BIGINT PRIMARY KEY, data TEXT NOT NULL)",
    "CREATE TABLE IF NOT EXISTS custom_words (chat_id BIGINT NOT NULL, word TEXT NOT NULL, PRIMARY KEY (chat_id, word))",
    """CREATE TABLE IF NOT EXISTS player_stats (
        chat_id BIGINT NOT NULL, user_id BIGINT NOT NULL, name TEXT NOT NULL,
        games INTEGER NOT NULL DEFAULT 0, wins INTEGER NOT NULL DEFAULT 0,
        spy_games INTEGER NOT NULL DEFAULT 0, spy_wins INTEGER NOT NULL DEFAULT 0,
        correct_votes INTEGER NOT NULL DEFAULT 0,
        PRIMARY KEY (chat_id, user_id))""",
    "CREATE TABLE IF NOT EXISTS game_snapshots (chat_id BIGINT PRIMARY KEY, data TEXT NOT NULL)",
]


class Storage:
    def __init__(self, database_url: str | None, sqlite_path: str = "spybot.db"):
        self.database_url = database_url or ""
        self.sqlite_path = sqlite_path
        self.pg = None
        self.sqlite: sqlite3.Connection | None = None
        self._lock = asyncio.Lock()

    @property
    def kind(self) -> str:
        return "postgres" if self.pg else "sqlite"

    async def init(self) -> None:
        if self.database_url.startswith(("postgres://", "postgresql://")):
            import asyncpg  # импорт здесь, чтобы без Postgres библиотека не требовалась
            self.pg = await asyncpg.create_pool(self.database_url, min_size=1, max_size=3)
        else:
            self.sqlite = sqlite3.connect(self.sqlite_path, check_same_thread=False)
            self.sqlite.row_factory = sqlite3.Row
        for stmt in SCHEMA:
            await self._execute(stmt)
        log.info("Хранилище: %s", self.kind)

    async def close(self) -> None:
        if self.pg:
            await self.pg.close()
        if self.sqlite:
            self.sqlite.close()

    # ---- низкоуровневые запросы (плейсхолдеры в стиле SQLite: ?) ----

    @staticmethod
    def _pg_sql(sql: str) -> str:
        counter = iter(range(1, 1000))
        return re.sub(r"\?", lambda _: f"${next(counter)}", sql)

    async def _execute(self, sql: str, *args) -> None:
        if self.pg:
            await self.pg.execute(self._pg_sql(sql), *args)
            return
        async with self._lock:
            await asyncio.to_thread(self._sqlite_run, sql, args, False)

    async def _fetch(self, sql: str, *args) -> list[dict]:
        if self.pg:
            rows = await self.pg.fetch(self._pg_sql(sql), *args)
            return [dict(r) for r in rows]
        async with self._lock:
            return await asyncio.to_thread(self._sqlite_run, sql, args, True)

    def _sqlite_run(self, sql: str, args: tuple, fetch: bool):
        cur = self.sqlite.execute(sql, args)
        rows = [dict(r) for r in cur.fetchall()] if fetch else None
        self.sqlite.commit()
        return rows

    # ---- настройки чата ----

    async def get_settings(self, chat_id: int) -> dict:
        rows = await self._fetch("SELECT data FROM chat_settings WHERE chat_id = ?", chat_id)
        return json.loads(rows[0]["data"]) if rows else {}

    async def save_settings(self, chat_id: int, data: dict) -> None:
        await self._execute(
            "INSERT INTO chat_settings (chat_id, data) VALUES (?, ?) "
            "ON CONFLICT (chat_id) DO UPDATE SET data = excluded.data",
            chat_id, json.dumps(data, ensure_ascii=False))

    # ---- свои слова ----

    async def list_words(self, chat_id: int) -> list[str]:
        rows = await self._fetch("SELECT word FROM custom_words WHERE chat_id = ? ORDER BY word", chat_id)
        return [r["word"] for r in rows]

    async def add_words(self, chat_id: int, words: list[str]) -> None:
        for w in words:
            await self._execute(
                "INSERT INTO custom_words (chat_id, word) VALUES (?, ?) ON CONFLICT DO NOTHING", chat_id, w)

    async def remove_word(self, chat_id: int, word: str) -> None:
        await self._execute("DELETE FROM custom_words WHERE chat_id = ? AND word = ?", chat_id, word)

    # ---- статистика ----

    async def record_results(self, chat_id: int, results: list[dict]) -> None:
        """results: [{user_id, name, won, spy, correct_votes}]"""
        for r in results:
            await self._execute(
                "INSERT INTO player_stats (chat_id, user_id, name, games, wins, spy_games, spy_wins, correct_votes) "
                "VALUES (?, ?, ?, 1, ?, ?, ?, ?) "
                "ON CONFLICT (chat_id, user_id) DO UPDATE SET "
                "name = excluded.name, games = player_stats.games + 1, wins = player_stats.wins + excluded.wins, "
                "spy_games = player_stats.spy_games + excluded.spy_games, "
                "spy_wins = player_stats.spy_wins + excluded.spy_wins, "
                "correct_votes = player_stats.correct_votes + excluded.correct_votes",
                chat_id, r["user_id"], r["name"], int(r["won"]), int(r["spy"]),
                int(r["spy"] and r["won"]), r["correct_votes"])

    async def user_stats(self, user_id: int, chat_id: int | None = None) -> dict | None:
        sql = ("SELECT SUM(games) AS games, SUM(wins) AS wins, SUM(spy_games) AS spy_games, "
               "SUM(spy_wins) AS spy_wins, SUM(correct_votes) AS correct_votes FROM player_stats WHERE user_id = ?")
        args = [user_id]
        if chat_id is not None:
            sql += " AND chat_id = ?"
            args.append(chat_id)
        rows = await self._fetch(sql, *args)
        if not rows or not rows[0]["games"]:
            return None
        return {k: int(v or 0) for k, v in rows[0].items()}

    async def top(self, chat_id: int, limit: int = 10) -> list[dict]:
        return await self._fetch(
            "SELECT user_id, name, games, wins, spy_wins, correct_votes FROM player_stats "
            "WHERE chat_id = ? ORDER BY wins DESC, correct_votes DESC, games ASC LIMIT ?", chat_id, limit)

    # ---- снимки игр ----

    async def save_snapshot(self, chat_id: int, data: dict) -> None:
        await self._execute(
            "INSERT INTO game_snapshots (chat_id, data) VALUES (?, ?) "
            "ON CONFLICT (chat_id) DO UPDATE SET data = excluded.data",
            chat_id, json.dumps(data, ensure_ascii=False))

    async def delete_snapshot(self, chat_id: int) -> None:
        await self._execute("DELETE FROM game_snapshots WHERE chat_id = ?", chat_id)

    async def load_snapshots(self) -> list[dict]:
        rows = await self._fetch("SELECT data FROM game_snapshots")
        return [json.loads(r["data"]) for r in rows]
