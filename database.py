"""
SQLite storage layer for the mahjong club bot.

Schema:
    players        -- one row per Discord user who has ever played
    games          -- one row per logged hand/round
    game_scores    -- one row per player per game (the points delta),
                      used for leaderboard + stats aggregation
"""

import aiosqlite
import os
import time

# Defaults to a local file for local testing. On Railway, set the
# DB_DIR environment variable to your mounted volume path (e.g. /app/data)
# so the database survives redeploys.
DB_DIR = os.getenv("DB_DIR", ".")
DB_PATH = os.path.join(DB_DIR, "mahjong.db")

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

CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT
);
"""


async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.executescript(SCHEMA)
        await db.commit()
        await _run_migrations(db)


async def _run_migrations(db):
    """Safely add columns to tables that may already exist from an
    earlier version of the bot, without erroring if already applied."""
    try:
        await db.execute(
            "ALTER TABLE players ADD COLUMN blacklisted INTEGER NOT NULL DEFAULT 0"
        )
        await db.commit()
    except Exception as e:
        if "duplicate column" not in str(e).lower():
            raise


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


async def get_setting(key: str):
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT value FROM settings WHERE key = ?", (key,)
        ) as cur:
            row = await cur.fetchone()
            return row[0] if row else None


async def set_setting(key: str, value: str):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO settings (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )
        await db.commit()


async def set_blacklisted(discord_id: str, blacklisted: bool) -> bool:
    """Returns True if a player record was found and updated."""
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "UPDATE players SET blacklisted = ? WHERE discord_id = ?",
            (1 if blacklisted else 0, discord_id),
        )
        await db.commit()
        return cur.rowcount > 0


async def is_blacklisted(discord_id: str) -> bool:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT blacklisted FROM players WHERE discord_id = ?", (discord_id,)
        ) as cur:
            row = await cur.fetchone()
            return bool(row and row[0])


async def get_blacklisted_players():
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT display_name, discord_id FROM players WHERE blacklisted = 1"
        ) as cur:
            return await cur.fetchall()


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


async def get_game(game_id: int):
    """Full detail for one game, used to show a confirmation before delete/edit."""
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            """
            SELECT g.id, g.timestamp, g.win_type, g.faan,
                   winner.display_name, discarder.display_name, g.notes
            FROM games g
            LEFT JOIN players winner ON winner.id = g.winner_id
            LEFT JOIN players discarder ON discarder.id = g.discarder_id
            WHERE g.id = ?
            """,
            (game_id,),
        ) as cur:
            game_row = await cur.fetchone()
        if not game_row:
            return None

        async with db.execute(
            """
            SELECT p.display_name, gs.points
            FROM game_scores gs
            JOIN players p ON p.id = gs.player_id
            WHERE gs.game_id = ?
            """,
            (game_id,),
        ) as cur:
            score_rows = await cur.fetchall()

        return {
            "id": game_row[0],
            "timestamp": game_row[1],
            "win_type": game_row[2],
            "faan": game_row[3],
            "winner_name": game_row[4],
            "discarder_name": game_row[5],
            "notes": game_row[6],
            "scores": score_rows,  # list of (display_name, points)
        }


async def delete_game(game_id: int) -> bool:
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT id FROM games WHERE id = ?", (game_id,))
        if not await cur.fetchone():
            return False
        await db.execute("DELETE FROM game_scores WHERE game_id = ?", (game_id,))
        await db.execute("DELETE FROM games WHERE id = ?", (game_id,))
        await db.commit()
        return True


async def update_game_faan(game_id: int, new_faan: int, new_deltas: dict) -> bool:
    """Corrects the faan count on an existing discard/self_draw game and
    replaces its score deltas. new_deltas: {player_id: points_delta}
    Keeps the same seated players, winner, and win_type."""
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT id FROM games WHERE id = ?", (game_id,))
        if not await cur.fetchone():
            return False

        await db.execute(
            "UPDATE games SET faan = ? WHERE id = ?", (new_faan, game_id)
        )
        await db.execute("DELETE FROM game_scores WHERE game_id = ?", (game_id,))
        for player_id, points in new_deltas.items():
            await db.execute(
                "INSERT INTO game_scores (game_id, player_id, points) VALUES (?, ?, ?)",
                (game_id, player_id, points),
            )
        await db.commit()
        return True


async def get_game_player_ids(game_id: int):
    """Returns {win_type, winner_id, discarder_id, player_ids: [4 ids]}
    or None if the game doesn't exist. Used to recompute scores on edit."""
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT win_type, winner_id, discarder_id FROM games WHERE id = ?",
            (game_id,),
        ) as cur:
            row = await cur.fetchone()
        if not row:
            return None
        win_type, winner_id, discarder_id = row

        async with db.execute(
            "SELECT player_id FROM game_scores WHERE game_id = ?", (game_id,)
        ) as cur:
            player_ids = [r[0] for r in await cur.fetchall()]

        return {
            "win_type": win_type,
            "winner_id": winner_id,
            "discarder_id": discarder_id,
            "player_ids": player_ids,
        }


async def export_rows():
    """One row per player per game, long format -- suitable for CSV export
    into Power BI / Tableau / Excel."""
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            """
            SELECT g.id AS game_id,
                   g.timestamp,
                   g.win_type,
                   g.faan,
                   p.display_name AS player_name,
                   p.discord_id,
                   gs.points,
                   CASE WHEN g.winner_id = gs.player_id THEN 1 ELSE 0 END AS is_winner,
                   CASE WHEN g.discarder_id = gs.player_id THEN 1 ELSE 0 END AS is_discarder,
                   g.notes
            FROM game_scores gs
            JOIN games g ON g.id = gs.game_id
            JOIN players p ON p.id = gs.player_id
            ORDER BY g.timestamp ASC, g.id ASC
            """
        ) as cur:
            return await cur.fetchall()


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
