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
    timestamp REAL NOT NULL,      -- when it was logged (for ordering/recent-games)
    game_date TEXT,               -- user-entered date of play, YYYY-MM-DD
    win_type TEXT NOT NULL,       -- 'discard' | 'self_draw' | 'false_win' | 'draw'
    faan INTEGER,                 -- null for draw / false_win
    winner_id INTEGER,            -- null for draw
    discarder_id INTEGER,         -- set only for discard wins
    season_id INTEGER,            -- which season this game counts toward
    season_game_number INTEGER,   -- this game's position WITHIN its season (resets each season) -- display only, "id" above remains the true unique key used for delete/edit
    logged_by TEXT NOT NULL,      -- discord_id of whoever ran the command
    notes TEXT
);

CREATE TABLE IF NOT EXISTS seasons (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    season_number INTEGER,
    name TEXT NOT NULL,           -- e.g. "Fall 2026"
    start_date TEXT,              -- YYYY-MM-DD, optional -- locks submissions before this date
    end_date TEXT,                -- YYYY-MM-DD, optional -- locks submissions after this date
    is_active INTEGER NOT NULL DEFAULT 0,
    created_at REAL NOT NULL,
    created_by TEXT
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

    for stmt in (
        "ALTER TABLE games ADD COLUMN season_id INTEGER",
        "ALTER TABLE games ADD COLUMN game_date TEXT",
    ):
        try:
            await db.execute(stmt)
            await db.commit()
        except Exception as e:
            if "duplicate column" not in str(e).lower():
                raise

    for stmt in (
        "ALTER TABLE seasons ADD COLUMN season_number INTEGER",
        "ALTER TABLE seasons ADD COLUMN start_date TEXT",
        "ALTER TABLE seasons ADD COLUMN end_date TEXT",
    ):
        try:
            await db.execute(stmt)
            await db.commit()
        except Exception as e:
            if "duplicate column" not in str(e).lower():
                raise

    # Backfill season_number for any season rows that predate that column
    # (in creation order, so the earliest season becomes #1).
    async with db.execute(
        "SELECT id FROM seasons WHERE season_number IS NULL ORDER BY created_at ASC"
    ) as cur:
        missing = await cur.fetchall()
    if missing:
        async with db.execute("SELECT COALESCE(MAX(season_number), 0) FROM seasons") as cur:
            (next_number,) = await cur.fetchone()
        for (season_id,) in missing:
            next_number += 1
            await db.execute(
                "UPDATE seasons SET season_number = ? WHERE id = ?", (next_number, season_id)
            )
        await db.commit()

    # Ensure there's always exactly one active season. If none exists yet
    # (fresh install, or upgrading from a pre-season version of the bot),
    # create a default one and backfill any existing games into it.
    async with db.execute("SELECT id FROM seasons WHERE is_active = 1") as cur:
        active = await cur.fetchone()
    if not active:
        cur = await db.execute(
            "INSERT INTO seasons (season_number, name, is_active, created_at, created_by) VALUES (1, ?, 1, ?, ?)",
            ("Season 1", time.time(), "system"),
        )
        season_id = cur.lastrowid
        await db.execute(
            "UPDATE games SET season_id = ? WHERE season_id IS NULL", (season_id,)
        )
        await db.commit()

    try:
        await db.execute("ALTER TABLE games ADD COLUMN season_game_number INTEGER")
        await db.commit()
    except Exception as e:
        if "duplicate column" not in str(e).lower():
            raise

    # Backfill season_game_number for any games that predate that column,
    # numbering each season's games in the order they were originally
    # logged (by timestamp), so H1/H2/H3... start over cleanly per season.
    async with db.execute(
        "SELECT DISTINCT season_id FROM games WHERE season_game_number IS NULL"
    ) as cur:
        seasons_needing_backfill = [r[0] for r in await cur.fetchall()]
    for sid in seasons_needing_backfill:
        if sid is None:
            query, params = "SELECT id FROM games WHERE season_id IS NULL ORDER BY timestamp ASC, id ASC", ()
        else:
            query, params = "SELECT id FROM games WHERE season_id = ? ORDER BY timestamp ASC, id ASC", (sid,)
        async with db.execute(query, params) as cur:
            game_ids_in_order = [r[0] for r in await cur.fetchall()]
        for i, gid in enumerate(game_ids_in_order, start=1):
            await db.execute(
                "UPDATE games SET season_game_number = ? WHERE id = ?", (i, gid)
            )
    if seasons_needing_backfill:
        await db.commit()


async def get_all_seasons():
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT id, season_number, name, start_date, end_date, is_active FROM seasons ORDER BY season_number ASC"
        ) as cur:
            rows = await cur.fetchall()
            return [
                {"id": r[0], "number": r[1], "name": r[2], "start_date": r[3], "end_date": r[4], "is_active": bool(r[5])}
                for r in rows
            ]


async def get_season_by_number(season_number: int):
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT id, season_number, name, start_date, end_date, is_active FROM seasons WHERE season_number = ?",
            (season_number,),
        ) as cur:
            row = await cur.fetchone()
            if not row:
                return None
            return {"id": row[0], "number": row[1], "name": row[2], "start_date": row[3], "end_date": row[4], "is_active": bool(row[5])}


async def get_active_season():
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT id, season_number, name, start_date, end_date FROM seasons WHERE is_active = 1 LIMIT 1"
        ) as cur:
            row = await cur.fetchone()
            if not row:
                return None
            return {
                "id": row[0],
                "number": row[1],
                "name": row[2],
                "start_date": row[3],
                "end_date": row[4],
            }


async def create_new_season(
    name: str, created_by: str, season_number: int = None, start_date: str = None, end_date: str = None
) -> int:
    """Deactivates the current season and starts a new one. Past games
    stay tied to their original season_id, so history is preserved.
    If season_number is not given, auto-increments from the highest
    existing season number."""
    async with aiosqlite.connect(DB_PATH) as db:
        if season_number is None:
            async with db.execute("SELECT COALESCE(MAX(season_number), 0) FROM seasons") as cur:
                (max_num,) = await cur.fetchone()
            season_number = max_num + 1

        await db.execute("UPDATE seasons SET is_active = 0 WHERE is_active = 1")
        cur = await db.execute(
            """INSERT INTO seasons (season_number, name, start_date, end_date, is_active, created_at, created_by)
               VALUES (?, ?, ?, ?, 1, ?, ?)""",
            (season_number, name, start_date, end_date, time.time(), created_by),
        )
        await db.commit()
        return cur.lastrowid


async def update_season_dates(season_id: int, start_date: str, end_date: str):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE seasons SET start_date = ?, end_date = ? WHERE id = ?",
            (start_date, end_date, season_id),
        )
        await db.commit()


async def update_season_info(season_id: int, name: str, season_number: int, start_date: str, end_date: str):
    """Edits the current season's name/number/date-lock in place (does NOT
    create a new season or touch which games belong to it)."""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE seasons SET name = ?, season_number = ?, start_date = ?, end_date = ? WHERE id = ?",
            (name, season_number, start_date, end_date, season_id),
        )
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
    season_id: int = None,
    game_date: str = None,
):
    """score_deltas: {player_id: points_delta}
    Returns (game_id, season_game_number)."""
    async with aiosqlite.connect(DB_PATH) as db:
        if season_id is not None:
            async with db.execute(
                "SELECT COALESCE(MAX(season_game_number), 0) FROM games WHERE season_id = ?",
                (season_id,),
            ) as cur:
                (max_num,) = await cur.fetchone()
            season_game_number = max_num + 1
        else:
            season_game_number = None

        cur = await db.execute(
            """INSERT INTO games
               (timestamp, game_date, win_type, faan, winner_id, discarder_id, season_id, season_game_number, logged_by, notes)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                time.time(),
                game_date,
                win_type,
                faan,
                winner_player_id,
                discarder_player_id,
                season_id,
                season_game_number,
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
        return game_id, season_game_number


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


async def get_leaderboard(season_id: int = None):
    """Returns list of (display_name, total_points, games_played, wins, draws) sorted desc.
    If season_id is given, only counts games from that season."""
    async with aiosqlite.connect(DB_PATH) as db:
        if season_id is not None:
            query = """
                SELECT p.display_name,
                       COALESCE(SUM(gs.points), 0) AS total,
                       COUNT(gs.game_id) AS games_played,
                       COALESCE(SUM(CASE WHEN g.winner_id = p.id AND g.win_type IN ('discard','self_draw') THEN 1 ELSE 0 END), 0) AS wins,
                       COALESCE(SUM(CASE WHEN g.win_type = 'draw' THEN 1 ELSE 0 END), 0) AS draws
                FROM players p
                JOIN game_scores gs ON gs.player_id = p.id
                JOIN games g ON g.id = gs.game_id AND g.season_id = ?
                GROUP BY p.id
                ORDER BY total DESC, wins DESC
            """
            params = (season_id,)
        else:
            query = """
                SELECT p.display_name,
                       COALESCE(SUM(gs.points), 0) AS total,
                       COUNT(gs.game_id) AS games_played,
                       COALESCE(SUM(CASE WHEN g.winner_id = p.id AND g.win_type IN ('discard','self_draw') THEN 1 ELSE 0 END), 0) AS wins,
                       COALESCE(SUM(CASE WHEN g.win_type = 'draw' THEN 1 ELSE 0 END), 0) AS draws
                FROM players p
                LEFT JOIN game_scores gs ON gs.player_id = p.id
                LEFT JOIN games g ON g.id = gs.game_id
                GROUP BY p.id
                ORDER BY total DESC, wins DESC
            """
            params = ()
        async with db.execute(query, params) as cur:
            return await cur.fetchall()


def _compute_player_breakdown(games, player_id):
    """games: list of dicts {id, win_type, faan, winner_id, discarder_id,
    scores: [(player_id, display_name, points), ...]} for games the target
    player participated in. Replicates the original bot's feeding-stat logic:
    'fed by' / 'fed to' spans discard, self-draw, AND false-win hands."""
    wins = 0
    draws = 0
    total_win_points = 0
    false_wins = 0
    wins_by_type = {"discard": 0, "self_draw": 0}
    losses_by_type = {"discard": 0, "self_draw": 0}
    faan_dist_wins = {}
    faan_dist_losses = {}
    net_discard_given = 0
    fed_by = {}  # name -> points others fed this player
    fed_to = {}  # name -> points this player fed others
    total_points = 0
    biggest_win = 0
    biggest_loss = 0

    for g in games:
        mine = next((s for s in g["scores"] if s[0] == player_id), None)
        if not mine:
            continue
        my_points = mine[2]
        total_points += my_points

        is_winner = g["winner_id"] == player_id
        is_discarder = g["discarder_id"] == player_id

        if g["win_type"] == "draw":
            draws += 1
            continue  # draws involve no points/feeding at all

        if g["win_type"] == "false_win":
            if is_winner:  # "winner_id" stores the false-win caller
                false_wins += 1
            # Deliberately NOT `continue` here -- false-win payouts still
            # count toward the feeding stats below (matches the original
            # bot's behavior: fed-by/fed-to spans all three hand types).
        else:
            # discard / self_draw hands only
            if is_winner:
                wins += 1
                biggest_win = max(biggest_win, my_points)
                total_win_points += my_points
                if g["faan"] is not None:
                    faan_dist_wins[g["faan"]] = faan_dist_wins.get(g["faan"], 0) + 1
                wins_by_type["discard" if g["win_type"] == "discard" else "self_draw"] += 1
            elif my_points < 0:
                biggest_loss = min(biggest_loss, my_points)
                if g["faan"] is not None:
                    faan_dist_losses[g["faan"]] = faan_dist_losses.get(g["faan"], 0) + 1
                losses_by_type["discard" if g["win_type"] == "discard" else "self_draw"] += 1
                if g["win_type"] == "discard" and is_discarder:
                    net_discard_given += -my_points

        # Feeding relationships -- runs for discard, self_draw, AND false_win
        # hands (matches the original bot's logic exactly).
        positives = [s for s in g["scores"] if s[2] > 0]
        negatives = [s for s in g["scores"] if s[2] < 0]
        if my_points > 0:
            if len(positives) == 1:
                for _, name, pts in negatives:
                    fed_by[name] = fed_by.get(name, 0) + (-pts)
            elif len(negatives) == 1:
                name = negatives[0][1]
                fed_by[name] = fed_by.get(name, 0) + my_points
        elif my_points < 0:
            if len(negatives) == 1:
                for _, name, pts in positives:
                    fed_to[name] = fed_to.get(name, 0) + pts
            elif len(positives) == 1:
                name = positives[0][1]
                fed_to[name] = fed_to.get(name, 0) + (-my_points)

    hands = sum(1 for g in games if any(s[0] == player_id for s in g["scores"]))
    win_rate = (wins / hands * 100) if hands else 0
    avg_per_hand = (total_points / hands) if hands else 0

    top_n = lambda d: sorted(d.items(), key=lambda kv: kv[1], reverse=True)[:5]

    return {
        "hands": hands,
        "wins": wins,
        "draws": draws,
        "false_wins": false_wins,
        "win_rate": win_rate,
        "avg_per_hand": avg_per_hand,
        "total_points": total_points,
        "biggest_win": biggest_win,
        "total_win_points": total_win_points,
        "biggest_loss": biggest_loss,
        "net_discard_given": net_discard_given,
        "wins_by_type": wins_by_type,
        "losses_by_type": losses_by_type,
        "faan_dist_wins": sorted(faan_dist_wins.items()),
        "faan_dist_losses": sorted(faan_dist_losses.items()),
        "fed_by": top_n(fed_by),
        "fed_to": top_n(fed_to),
        "fed_by_all": fed_by,
        "fed_to_all": fed_to,
    }


async def _fetch_scoped_games_for_player(db, player_id: int, season_id: int = None):
    """Fetch every game this player was seated in (optionally scoped to a
    season), each with the full 4-player score breakdown needed for the
    feeding-stat calculation."""
    if season_id is not None:
        game_id_query = """
            SELECT DISTINCT g.id, g.win_type, g.faan, g.winner_id, g.discarder_id
            FROM games g
            JOIN game_scores gs ON gs.game_id = g.id
            WHERE gs.player_id = ? AND g.season_id = ?
        """
        params = (player_id, season_id)
    else:
        game_id_query = """
            SELECT DISTINCT g.id, g.win_type, g.faan, g.winner_id, g.discarder_id
            FROM games g
            JOIN game_scores gs ON gs.game_id = g.id
            WHERE gs.player_id = ?
        """
        params = (player_id,)

    async with db.execute(game_id_query, params) as cur:
        game_rows = await cur.fetchall()

    games = []
    for game_id, win_type, faan, winner_id, discarder_id in game_rows:
        async with db.execute(
            """SELECT p.id, p.display_name, gs.points
               FROM game_scores gs JOIN players p ON p.id = gs.player_id
               WHERE gs.game_id = ?""",
            (game_id,),
        ) as cur:
            scores = await cur.fetchall()
        games.append(
            {
                "id": game_id,
                "win_type": win_type,
                "faan": faan,
                "winner_id": winner_id,
                "discarder_id": discarder_id,
                "scores": scores,
            }
        )
    return games


async def get_player_full_stats(discord_id: str, season_id: int = None):
    """Full stat card data for one player, scoped to a season if given,
    or lifetime if season_id is None."""
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT id, display_name FROM players WHERE discord_id = ?", (discord_id,)
        ) as cur:
            row = await cur.fetchone()
        if not row:
            return None
        player_id, display_name = row

        games = await _fetch_scoped_games_for_player(db, player_id, season_id)
        if not games:
            return {"display_name": display_name, "hands": 0}

        breakdown = _compute_player_breakdown(games, player_id)
        breakdown["display_name"] = display_name

        # Rank within this scope
        board = await get_leaderboard(season_id)
        names_ranked = [row[0] for row in board]
        breakdown["rank"] = (
            names_ranked.index(display_name) + 1 if display_name in names_ranked else None
        )
        breakdown["total_in_scope"] = len(names_ranked)

        return breakdown


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


async def get_recent_games_for_picker(limit: int = 25):
    """Compact list for the mod-tools game picker dropdown (max 25 options)."""
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            """
            SELECT g.id, g.win_type, g.faan, g.game_date, winner.display_name, discarder.display_name,
                   s.season_number, g.season_game_number
            FROM games g
            LEFT JOIN players winner ON winner.id = g.winner_id
            LEFT JOIN players discarder ON discarder.id = g.discarder_id
            LEFT JOIN seasons s ON s.id = g.season_id
            ORDER BY g.timestamp DESC
            LIMIT ?
            """,
            (limit,),
        ) as cur:
            return await cur.fetchall()


async def get_game_for_edit(game_id: int):
    """Full detail needed to pre-fill the edit flow, including each seated
    player's Discord ID (so we can look up live discord.Member objects)."""
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            """SELECT g.id, g.win_type, g.faan, g.game_date, g.notes, g.winner_id, g.discarder_id,
                      g.season_id, g.season_game_number, s.season_number, s.name
               FROM games g LEFT JOIN seasons s ON s.id = g.season_id
               WHERE g.id = ?""",
            (game_id,),
        ) as cur:
            row = await cur.fetchone()
        if not row:
            return None
        gid, win_type, faan, game_date, notes, winner_id, discarder_id, season_id, season_game_number, season_number, season_name = row

        async with db.execute(
            """SELECT p.id, p.discord_id, p.display_name
               FROM game_scores gs JOIN players p ON p.id = gs.player_id
               WHERE gs.game_id = ?""",
            (game_id,),
        ) as cur:
            players = await cur.fetchall()  # [(player_id, discord_id, display_name), ...]

        return {
            "id": gid,
            "win_type": win_type,
            "faan": faan,
            "game_date": game_date,
            "notes": notes,
            "winner_id": winner_id,
            "discarder_id": discarder_id,
            "season_id": season_id,
            "season_game_number": season_game_number,
            "season_number": season_number,
            "season_name": season_name,
            "players": players,
        }


async def overwrite_game(
    game_id: int,
    win_type: str,
    faan,
    winner_player_id,
    discarder_player_id,
    notes: str,
    game_date: str,
    score_deltas: dict,
) -> bool:
    """Replaces an existing game's data and score rows in place (used by
    the full edit flow). Keeps the original season_id, timestamp, and
    logged_by untouched."""
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT id FROM games WHERE id = ?", (game_id,))
        if not await cur.fetchone():
            return False

        await db.execute(
            """UPDATE games SET win_type = ?, faan = ?, winner_id = ?, discarder_id = ?,
               notes = ?, game_date = ? WHERE id = ?""",
            (win_type, faan, winner_player_id, discarder_player_id, notes, game_date, game_id),
        )
        await db.execute("DELETE FROM game_scores WHERE game_id = ?", (game_id,))
        for player_id, points in score_deltas.items():
            await db.execute(
                "INSERT INTO game_scores (game_id, player_id, points) VALUES (?, ?, ?)",
                (game_id, player_id, points),
            )
        await db.commit()
        return True


async def get_game(game_id: int):
    """Full detail for one game, used to show a confirmation before delete/edit."""
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            """
            SELECT g.id, g.timestamp, g.win_type, g.faan,
                   winner.display_name, discarder.display_name, g.notes,
                   g.season_game_number, s.season_number
            FROM games g
            LEFT JOIN players winner ON winner.id = g.winner_id
            LEFT JOIN players discarder ON discarder.id = g.discarder_id
            LEFT JOIN seasons s ON s.id = g.season_id
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
            "season_game_number": game_row[7],
            "season_number": game_row[8],
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


async def get_player_game_history(discord_id: str, season_id: int, limit: int = 25):
    """This player's games within one season, most recent first, with
    enough detail to describe the outcome from their point of view."""
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            """
            SELECT g.id, g.season_game_number, g.game_date, g.win_type, g.faan,
                   gs.points,
                   CASE WHEN g.winner_id = gs.player_id THEN 1 ELSE 0 END AS is_winner,
                   CASE WHEN g.discarder_id = gs.player_id THEN 1 ELSE 0 END AS is_discarder
            FROM game_scores gs
            JOIN games g ON g.id = gs.game_id
            JOIN players p ON p.id = gs.player_id
            WHERE p.discord_id = ? AND g.season_id = ?
            ORDER BY g.timestamp DESC
            LIMIT ?
            """,
            (discord_id, season_id, limit),
        ) as cur:
            return await cur.fetchall()


def _feeding_amount(scores, winner_id, payer_id):
    """Points specifically paid by payer_id toward winner_id's gain in this
    one hand, or 0 if payer_id wasn't a direct contributor to that win.
    scores: list of (player_id, display_name, points) for all 4 seats."""
    mine_winner = next((s[2] for s in scores if s[0] == winner_id), None)
    if mine_winner is None or mine_winner <= 0:
        return 0
    positives = [s for s in scores if s[2] > 0]
    negatives = [s for s in scores if s[2] < 0]
    if len(positives) == 1:
        p = next((s for s in negatives if s[0] == payer_id), None)
        return -p[2] if p else 0
    elif len(negatives) == 1:
        return mine_winner if negatives[0][0] == payer_id else 0
    return 0


async def get_head_to_head_detail(discord_id_a: str, discord_id_b: str, season_id: int = None):
    """Everything needed for a rich /head-to-head card, scoped to a season
    if given, or lifetime if season_id is None."""
    empty = {
        "games_together": 0, "draws_together": 0, "wins_a": 0, "wins_b": 0,
        "points_a_from_b": 0, "points_b_from_a": 0, "times_a_fed_b": 0, "times_b_fed_a": 0,
        "biggest_win_a": 0, "biggest_win_b": 0,
    }
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT id FROM players WHERE discord_id = ?", (discord_id_a,)) as cur:
            row_a = await cur.fetchone()
        async with db.execute("SELECT id FROM players WHERE discord_id = ?", (discord_id_b,)) as cur:
            row_b = await cur.fetchone()
        if not row_a or not row_b:
            return empty
        player_id_a, player_id_b = row_a[0], row_b[0]

        games_a = await _fetch_scoped_games_for_player(db, player_id_a, season_id)
        shared = [g for g in games_a if any(s[0] == player_id_b for s in g["scores"])]

        result = dict(empty)
        result["games_together"] = len(shared)

        for g in shared:
            if g["win_type"] == "draw":
                result["draws_together"] += 1
                continue

            mine_a = next(s[2] for s in g["scores"] if s[0] == player_id_a)
            mine_b = next(s[2] for s in g["scores"] if s[0] == player_id_b)

            if g["winner_id"] == player_id_a:
                result["wins_a"] += 1
                result["biggest_win_a"] = max(result["biggest_win_a"], mine_a)
            if g["winner_id"] == player_id_b:
                result["wins_b"] += 1
                result["biggest_win_b"] = max(result["biggest_win_b"], mine_b)

            amt_b_from_a = _feeding_amount(g["scores"], player_id_b, player_id_a)
            if amt_b_from_a:
                result["points_b_from_a"] += amt_b_from_a
                result["times_a_fed_b"] += 1

            amt_a_from_b = _feeding_amount(g["scores"], player_id_a, player_id_b)
            if amt_a_from_b:
                result["points_a_from_b"] += amt_a_from_b
                result["times_b_fed_a"] += 1

        return result


async def export_rows():
    """One row per player per game, long format -- suitable for CSV export
    into Power BI / Tableau / Excel. Includes season info for filtering."""
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            """
            SELECT g.id AS game_id,
                   g.timestamp,
                   g.game_date,
                   s.season_number,
                   s.name AS season_name,
                   g.season_game_number,
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
            LEFT JOIN seasons s ON s.id = g.season_id
            ORDER BY g.timestamp ASC, g.id ASC
            """
        ) as cur:
            return await cur.fetchall()


async def get_recent_games(limit: int = 10):
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            """
            SELECT g.id, g.timestamp, g.win_type, g.faan,
                   winner.display_name, discarder.display_name,
                   s.season_number, g.season_game_number
            FROM games g
            LEFT JOIN players winner ON winner.id = g.winner_id
            LEFT JOIN players discarder ON discarder.id = g.discarder_id
            LEFT JOIN seasons s ON s.id = g.season_id
            ORDER BY g.timestamp DESC
            LIMIT ?
            """,
            (limit,),
        ) as cur:
            return await cur.fetchall()
