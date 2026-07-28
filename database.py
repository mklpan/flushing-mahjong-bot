"""
SQLite storage layer for the mahjong club bot.

Schema:
    players        -- one row per Discord user who has ever played
    games          -- one row per logged hand/round
    game_scores    -- one row per player per game (the points delta),
                      used for leaderboard + stats aggregation
"""

import aiosqlite
import time

DB_PATH = "mahjong.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS players (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    discord_id TEXT UNIQUE NOT NULL,
    display_name TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS games (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp REAL NOT NULL,
    win_type TEXT NOT NULL,       -- 'discard' | 'self_draw' | 'false_win' | 'draw'
    faan INTEGER,                 -- null for draw / false_win
    winner_id INTEGER,            -- null for draw
    discarder_id INTEGER,         -- set only for discard wins
    logged_by TEXT NOT NULL,      -- discord_id of whoever ran the command
    notes TEXT
);

CREATE TABLE IF NOT EXISTS game_scores (
    game_id INTEGER NOT NULL,
    player_id INTEGER NOT NULL,
    points INTEGER NOT NULL,
    FOREIGN KEY (game_id) REFERENCES games(id),
    FOREIGN KEY (player_id) REFERENCES players(id)
);
"""


async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.executescript(SCHEMA)
        await db.commit()


async def get_or_create_player(discord_id: str, display_name: str) -> int:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT id FROM players WHERE discord_id = ?", (discord_id,)
        ) as cur:
            row = await cur.fetchone()
        if row:
            # keep display name fresh in case of nickname changes
            await db.execute(
                "UPDATE players SET display_name = ? WHERE discord_id = ?",
                (display_name, discord_id),
            )
            await db.commit()
            return row[0]

        cur = await db.execute(
            "INSERT INTO players (discord_id, display_name) VALUES (?, ?)",
            (discord_id, display_name),
        )
        await db.commit()
        return cur.lastrowid


async def record_game(
    win_type: str,
    faan,
    winner_player_id,
    discarder_player_id,
    logged_by: str,
    score_deltas: dict,
    notes: str = None,
) -> int:
    """score_deltas: {player_id: points_delta}"""
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            """INSERT INTO games
               (timestamp, win_type, faan, winner_id, discarder_id, logged_by, notes)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (
                time.time(),
                win_type,
                faan,
                winner_player_id,
                discarder_player_id,
                logged_by,
                notes,
            ),
        )
        game_id = cur.lastrowid

        for player_id, points in score_deltas.items():
            await db.execute(
                "INSERT INTO game_scores (game_id, player_id, points) VALUES (?, ?, ?)",
                (game_id, player_id, points),
            )

        await db.commit()
        return game_id


async def get_leaderboard():
    """Returns list of (display_name, total_points, games_played) sorted desc."""
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            """
            SELECT p.display_name,
                   COALESCE(SUM(gs.points), 0) AS total,
                   COUNT(gs.game_id) AS games_played
            FROM players p
            LEFT JOIN game_scores gs ON gs.player_id = p.id
            GROUP BY p.id
            ORDER BY total DESC
            """
        ) as cur:
            return await cur.fetchall()


async def get_player_stats(discord_id: str):
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT id, display_name FROM players WHERE discord_id = ?",
            (discord_id,),
        ) as cur:
            row = await cur.fetchone()
        if not row:
            return None
        player_id, display_name = row

        async with db.execute(
            """SELECT COUNT(*), COALESCE(SUM(points), 0)
               FROM game_scores WHERE player_id = ?""",
            (player_id,),
        ) as cur:
            games_played, total_points = await cur.fetchone()

        async with db.execute(
            "SELECT COUNT(*) FROM games WHERE winner_id = ?",
            (player_id,),
        ) as cur:
            (wins,) = await cur.fetchone()

        async with db.execute(
            """SELECT AVG(faan) FROM games
               WHERE winner_id = ? AND win_type IN ('discard', 'self_draw')""",
            (player_id,),
        ) as cur:
            (avg_faan,) = await cur.fetchone()

        async with db.execute(
            """SELECT COUNT(*) FROM games
               WHERE win_type = 'false_win' AND winner_id = ?""",
            (player_id,),
        ) as cur:
            (false_wins,) = await cur.fetchone()

        return {
            "display_name": display_name,
            "games_played": games_played,
            "total_points": total_points,
            "wins": wins,
            "win_rate": (wins / games_played * 100) if games_played else 0,
            "avg_faan": round(avg_faan, 1) if avg_faan else None,
            "false_wins": false_wins,
        }


async def get_recent_games(limit: int = 10):
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            """
            SELECT g.id, g.timestamp, g.win_type, g.faan,
                   winner.display_name, discarder.display_name
            FROM games g
            LEFT JOIN players winner ON winner.id = g.winner_id
            LEFT JOIN players discarder ON discarder.id = g.discarder_id
            ORDER BY g.timestamp DESC
            LIMIT ?
            """,
            (limit,),
        ) as cur:
            return await cur.fetchall()
