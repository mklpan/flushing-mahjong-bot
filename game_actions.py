"""
Shared game-logging and embed-formatting logic used by both the slash
commands and the button/modal UI, so scoring/validation/formatting all
live in exactly one place.
"""

import datetime
import io
import discord

import matplotlib
matplotlib.use("Agg")  # headless -- no display available on the server
import matplotlib.pyplot as plt
import numpy as np

import database as db
import scoring

CLUB_NAME = "Flushing Mahjong League"
PLAYSTYLE_MIN_HANDS = 20

WIN_TYPE_LABELS = {
    "discard": "Discard",
    "self_draw": "Self-Draw",
    "false_win": "False Win",
    "draw": "Draw",
}

EMBED_COLORS = {
    "discard": discord.Color.green(),
    "self_draw": discord.Color.green(),
    "false_win": discord.Color.red(),
    "draw": discord.Color.yellow(),
}


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

async def perform_log_game(
    seated,
    win_type: str,
    winner=None,
    faan=None,
    discarder=None,
    false_win_caller=None,
    notes: str = None,
    logged_by_id: str = None,
    logged_by_name: str = None,
    game_date: str = None,
):
    """seated: list of 4 discord.Member (or User) objects.
    Returns {"ok": True, "game_id": int, "embed": discord.Embed}
    or {"ok": False, "error": str}."""

    if len(set(m.id for m in seated)) != 4:
        return {"ok": False, "error": "All 4 seats must be different players."}

    for m in seated:
        if await db.is_blacklisted(str(m.id)):
            return {
                "ok": False,
                "error": f"{m.display_name} is blacklisted from logging games. "
                "Ask a mod to unblacklist them first.",
            }

    try:
        if win_type in ("discard", "self_draw"):
            if winner is None or faan is None:
                return {
                    "ok": False,
                    "error": "Discard and self-draw wins require both a winner and a faan count.",
                }
            if winner not in seated:
                return {"ok": False, "error": "Winner must be one of the 4 seated players."}
            scoring.validate_win_faan(faan)
            faan = scoring.clamp_faan(faan)

        if win_type == "discard":
            if discarder is None:
                return {"ok": False, "error": "Discard wins require a discarder."}
            if discarder not in seated or discarder == winner:
                return {
                    "ok": False,
                    "error": "Discarder must be one of the other 3 seated players.",
                }

        if win_type == "false_win":
            if false_win_caller is None or false_win_caller not in seated:
                return {
                    "ok": False,
                    "error": "False wins require the false-win caller to be one of the 4 seated players.",
                }

        if win_type not in ("discard", "self_draw", "false_win", "draw"):
            return {"ok": False, "error": f"Unknown win type: {win_type}"}

    except scoring.ScoringError as e:
        return {"ok": False, "error": str(e)}

    player_ids = {}
    for m in seated:
        player_ids[m.id] = await db.get_or_create_player(str(m.id), m.display_name)

    deltas = {player_ids[m.id]: 0 for m in seated}
    winner_id = discarder_id = None

    if win_type == "discard":
        result = scoring.score_discard_win(faan)
        winner_id = player_ids[winner.id]
        discarder_id = player_ids[discarder.id]
        deltas[winner_id] += result["winner"]
        deltas[discarder_id] += result["discarder"]

    elif win_type == "self_draw":
        result = scoring.score_self_draw_win(faan)
        winner_id = player_ids[winner.id]
        deltas[winner_id] += result["winner"]
        for m in seated:
            if m.id != winner.id:
                deltas[player_ids[m.id]] += result["each_opponent"]

    elif win_type == "false_win":
        result = scoring.score_false_win()
        winner_id = player_ids[false_win_caller.id]
        deltas[winner_id] += result["false_winner"]
        for m in seated:
            if m.id != false_win_caller.id:
                deltas[player_ids[m.id]] += result["each_opponent"]

    # draw: deltas stay 0

    season = await db.get_active_season()
    season_id = season["id"] if season else None

    if season and (season["start_date"] or season["end_date"]):
        check_date = game_date or datetime.date.today().isoformat()
        if season["start_date"] and check_date < season["start_date"]:
            return {
                "ok": False,
                "error": f"That date is before the **{season['name']}** season starts ({season['start_date']}).",
            }
        if season["end_date"] and check_date > season["end_date"]:
            return {
                "ok": False,
                "error": f"That date is after the **{season['name']}** season ends ({season['end_date']}).",
            }

    game_id, season_game_number = await db.record_game(
        win_type=win_type,
        faan=faan if win_type in ("discard", "self_draw") else None,
        winner_player_id=winner_id,
        discarder_player_id=discarder_id,
        logged_by=logged_by_id or "unknown",
        score_deltas=deltas,
        notes=notes,
        season_id=season_id,
        game_date=game_date,
    )

    resolved_winner = winner
    if win_type == "false_win":
        resolved_winner = false_win_caller

    embed = build_logged_game_embed(
        game_id=game_id,
        season_game_number=season_game_number,
        season=season,
        win_type=win_type,
        faan=faan,
        game_date=game_date,
        seated=seated,
        deltas={m: deltas[player_ids[m.id]] for m in seated},
        winner=resolved_winner,
        discarder=discarder if win_type == "discard" else None,
        logged_by_name=logged_by_name or logged_by_id or "unknown",
        notes=notes,
    )

    return {"ok": True, "game_id": game_id, "embed": embed}


async def perform_edit_game(
    game_id: int,
    seated,
    win_type: str,
    winner=None,
    faan=None,
    discarder=None,
    false_win_caller=None,
    notes: str = None,
    game_date: str = None,
    edited_by_name: str = None,
):
    """Same validation as perform_log_game, but overwrites an existing
    game in place instead of creating a new one. Does not re-check season
    date locks (the game may belong to a past, already-closed season)."""

    if len(set(m.id for m in seated)) != 4:
        return {"ok": False, "error": "All 4 seats must be different players."}

    try:
        if win_type in ("discard", "self_draw"):
            if winner is None or faan is None:
                return {"ok": False, "error": "Discard and self-draw wins require both a winner and a faan count."}
            if winner not in seated:
                return {"ok": False, "error": "Winner must be one of the 4 seated players."}
            scoring.validate_win_faan(faan)
            faan = scoring.clamp_faan(faan)

        if win_type == "discard":
            if discarder is None:
                return {"ok": False, "error": "Discard wins require a discarder."}
            if discarder not in seated or discarder == winner:
                return {"ok": False, "error": "Discarder must be one of the other 3 seated players."}

        if win_type == "false_win":
            if false_win_caller is None or false_win_caller not in seated:
                return {
                    "ok": False,
                    "error": "False wins require the false-win caller to be one of the 4 seated players.",
                }

        if win_type not in ("discard", "self_draw", "false_win", "draw"):
            return {"ok": False, "error": f"Unknown win type: {win_type}"}

    except scoring.ScoringError as e:
        return {"ok": False, "error": str(e)}

    player_ids = {}
    for m in seated:
        player_ids[m.id] = await db.get_or_create_player(str(m.id), m.display_name)

    deltas = {player_ids[m.id]: 0 for m in seated}
    winner_id = discarder_id = None

    if win_type == "discard":
        result = scoring.score_discard_win(faan)
        winner_id = player_ids[winner.id]
        discarder_id = player_ids[discarder.id]
        deltas[winner_id] += result["winner"]
        deltas[discarder_id] += result["discarder"]

    elif win_type == "self_draw":
        result = scoring.score_self_draw_win(faan)
        winner_id = player_ids[winner.id]
        deltas[winner_id] += result["winner"]
        for m in seated:
            if m.id != winner.id:
                deltas[player_ids[m.id]] += result["each_opponent"]

    elif win_type == "false_win":
        result = scoring.score_false_win()
        winner_id = player_ids[false_win_caller.id]
        deltas[winner_id] += result["false_winner"]
        for m in seated:
            if m.id != false_win_caller.id:
                deltas[player_ids[m.id]] += result["each_opponent"]

    ok = await db.overwrite_game(
        game_id=game_id,
        win_type=win_type,
        faan=faan if win_type in ("discard", "self_draw") else None,
        winner_player_id=winner_id,
        discarder_player_id=discarder_id,
        notes=notes,
        game_date=game_date,
        score_deltas=deltas,
    )
    if not ok:
        return {"ok": False, "error": f"Game #{game_id} no longer exists."}

    # Look up this game's own season info (not necessarily the currently
    # active season -- an old season's game can still be edited).
    edited_row = await db.get_game_for_edit(game_id)
    season_game_number = edited_row["season_game_number"] if edited_row else None
    season_for_display = None
    if edited_row and edited_row["season_number"] is not None:
        season_for_display = {"number": edited_row["season_number"], "name": edited_row["season_name"]}

    resolved_winner = winner if win_type != "false_win" else false_win_caller
    embed = build_logged_game_embed(
        game_id=game_id,
        season_game_number=season_game_number,
        season=season_for_display,
        win_type=win_type,
        faan=faan,
        game_date=game_date,
        seated=seated,
        deltas={m: deltas[player_ids[m.id]] for m in seated},
        winner=resolved_winner,
        discarder=discarder if win_type == "discard" else None,
        logged_by_name=f"{edited_by_name} (edited)" if edited_by_name else "(edited)",
        notes=notes,
    )
    display_number = season_game_number if season_game_number is not None else game_id
    embed.title = f"✏️ Edited H{display_number}"

    return {"ok": True, "game_id": game_id, "embed": embed}


def build_logged_game_embed(
    game_id, win_type, faan, game_date, seated, deltas, winner, discarder, logged_by_name, notes,
    season_game_number=None, season=None,
):
    """Matches the club's 'Logged H###' card format."""
    display_number = season_game_number if season_game_number is not None else game_id
    embed = discord.Embed(
        title=f"🀄 Logged H{display_number}",
        color=EMBED_COLORS.get(win_type, discord.Color.green()),
    )

    if season:
        embed.add_field(name="Season", value=f"Season {season['number']} ({season['name']})", inline=True)

    if winner is not None:
        winner_points = deltas[winner]
        sign = "+" if winner_points >= 0 else ""
        embed.add_field(name="Winner", value=f"{winner.display_name} ({sign}{winner_points})", inline=True)
    if faan is not None:
        embed.add_field(name="Faan", value=str(faan), inline=True)
    embed.add_field(name="Win Type", value=WIN_TYPE_LABELS.get(win_type, win_type), inline=True)

    embed.add_field(name="Date", value=game_date or datetime.date.today().isoformat(), inline=True)
    if discarder is not None:
        embed.add_field(name="Discarder", value=discarder.display_name, inline=True)

    lines = []
    for m in seated:
        d = deltas[m]
        sign = "+" if d >= 0 else ""
        if win_type == "false_win":
            if m == winner:
                lines.append(f"🚩 {m.display_name} — false win ({sign}{d})")
            else:
                lines.append(f"💰 {m.display_name} — fed ({sign}{d})")
        elif win_type == "draw":
            lines.append(f"🤝 {m.display_name} — draw (+0)")
        elif m == winner:
            lines.append(f"🏆 {m.display_name} — win ({sign}{d})")
        elif d == 0:
            lines.append(f"🛡️ {m.display_name} — safe (+0)")
        else:
            lines.append(f"💸 {m.display_name} — loss ({sign}{d})")
    embed.add_field(name="Results", value="\n".join(lines), inline=False)

    if notes:
        embed.add_field(name="Notes", value=notes, inline=False)

    footer_name = logged_by_name or "unknown"
    embed.set_footer(text=f"Logged by {footer_name}")
    embed.timestamp = datetime.datetime.now()
    return embed


async def post_logged_game(client: discord.Client, embed: discord.Embed):
    """Posts the logged-game card into the configured game-log channel, if
    one has been set up. Returns True if posted, False otherwise (e.g. not
    configured) -- callers should fall back to posting inline if False."""
    channel_id = await db.get_setting("gamelog_channel_id")
    if not channel_id:
        return False
    try:
        channel = client.get_channel(int(channel_id)) or await client.fetch_channel(int(channel_id))
        await channel.send(embed=embed)
        return True
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Leaderboard
# ---------------------------------------------------------------------------

def _format_leaderboard_lines(rows):
    lines = []
    for i, (name, total, hands, wins, draws) in enumerate(rows, start=1):
        medal = {1: "🥇", 2: "🥈", 3: "🥉"}.get(i, f"#{i}")
        win_rate = round(wins / hands * 100) if hands else 0
        draws_part = f" / {draws}D" if draws else ""
        lines.append(f"{medal} **{name}** : {total} pts · {wins}W{draws_part} / {hands} hands · {win_rate}%")
    return lines


async def build_leaderboard_embed(season: dict = None, lifetime: bool = False):
    if lifetime:
        title = f"🏆 {CLUB_NAME} — Lifetime Leaderboard"
        rows = await db.get_leaderboard(None)
        embed = discord.Embed(title=title, color=discord.Color.gold())
        embed.description = "\n".join(_format_leaderboard_lines(rows)) if rows else "No games logged yet. 🀄"
        embed.set_footer(text="Updates automatically after every logged game — all seasons combined")
        embed.timestamp = datetime.datetime.now()
        return embed

    if season is None:
        season = await db.get_active_season()
    if season:
        title = f"🏆 {CLUB_NAME} — Season {season['number']} Leaderboard ({season['name']})"
    else:
        title = f"🏆 {CLUB_NAME} — Leaderboard"
    season_id = season["id"] if season else None

    rows = await db.get_leaderboard(season_id)
    embed = discord.Embed(title=title, color=discord.Color.gold())
    if not rows:
        embed.description = "No games logged yet this season. 🀄"
    else:
        embed.description = "\n".join(_format_leaderboard_lines(rows))

    active_season = await db.get_active_season()
    is_current = active_season and season and active_season["id"] == season["id"]
    if is_current or season is None:
        embed.set_footer(text="Updates automatically after every logged game")
    else:
        embed.set_footer(text="Final standings — this season has ended")
    embed.timestamp = datetime.datetime.now()
    return embed


async def build_season_list_embed():
    seasons = await db.get_all_seasons()
    embed = discord.Embed(title=f"📅 {CLUB_NAME} — Seasons", color=discord.Color.blurple())
    if not seasons:
        embed.description = "No seasons yet."
        return embed
    lines = []
    for s in seasons:
        marker = " 🟢 *(current)*" if s["is_active"] else ""
        date_range = ""
        if s["start_date"] or s["end_date"]:
            date_range = f" · {s['start_date'] or 'open'} → {s['end_date'] or 'open'}"
        lines.append(f"**Season {s['number']}** — {s['name']}{date_range}{marker}")
    embed.description = "\n".join(lines)
    return embed


HOF_MEDALS = ["🥇", "🥈", "🥉"]


async def build_hall_of_fame_embed():
    """Top 10 from every COMPLETED season (the current in-progress season
    is deliberately excluded until it ends)."""
    seasons = await db.get_all_seasons()
    finished_seasons = [s for s in seasons if not s["is_active"]]

    embed = discord.Embed(title=f"🏛️ {CLUB_NAME} — Hall of Fame", color=discord.Color.dark_gold())
    if not finished_seasons:
        embed.description = "No completed seasons yet — check back once the current season ends!"
        embed.set_footer(text="Updates automatically")
        return embed

    lines = []
    for s in reversed(finished_seasons):  # most recent completed season first
        rows = await db.get_leaderboard(s["id"])
        header = f"**Season {s['number']} — {s['name']}**"
        if not rows:
            lines.append(f"{header}\n_no games logged_")
        else:
            top10 = rows[:10]
            lines.append(header + "\n" + "\n".join(_format_leaderboard_lines(top10)))
    embed.description = "\n\n".join(lines)
    embed.set_footer(text="Updates automatically — a season appears here once it ends")
    embed.timestamp = datetime.datetime.now()
    return embed


async def build_player_history_embed(discord_id: str, display_name: str, limit: int = 25):
    """This player's game history for the CURRENT season only."""
    season = await db.get_active_season()
    embed = discord.Embed(title=f"📜 Game History — {display_name}", color=discord.Color.blue())

    if not season:
        embed.description = "No active season."
        return embed

    embed.set_author(name=f"Season {season['number']} ({season['name']})")

    rows = await db.get_player_game_history(discord_id, season["id"], limit)
    if not rows:
        embed.description = "No games logged yet this season."
        return embed

    lines = []
    for game_id, season_game_number, game_date, win_type, faan, points, is_winner, is_discarder in rows:
        hand_ref = f"H{season_game_number}" if season_game_number is not None else f"H{game_id}"
        sign = "+" if points >= 0 else ""

        if win_type == "draw":
            icon, desc = "🤝", "Draw"
        elif win_type == "false_win":
            icon, desc = ("🚩", "Called a false win") if is_winner else ("💰", "Fed a false win")
        elif win_type == "discard":
            if is_winner:
                icon, desc = "🏆", f"Won (Discard, {faan}f)"
            elif is_discarder:
                icon, desc = "💸", f"Discarded into a loss ({faan}f)"
            else:
                icon, desc = "🛡️", "Safe"
        elif win_type == "self_draw":
            icon, desc = ("🏆", f"Self-drew ({faan}f)") if is_winner else ("💸", f"Paid a self-draw ({faan}f)")
        else:
            icon, desc = "❓", win_type

        lines.append(f"{icon} **{hand_ref}** · {game_date or '?'} — {desc} ({sign}{points})")

    embed.description = "\n".join(lines)
    legend = "🏆 Win · 💸 Loss · 🛡️ Safe · 🤝 Draw · 🚩 False win called · 💰 Fed false win"
    if len(rows) == limit:
        embed.set_footer(text=f"Showing the {limit} most recent games this season · {legend}")
    else:
        embed.set_footer(text=legend)
    return embed


async def _build_single_h2h_embed(id_a, name_a, id_b, name_b, season_id, scope_label):
    d = await db.get_head_to_head_detail(id_a, id_b, season_id)
    embed = discord.Embed(title=f"⚔️ {name_a} vs {name_b}", color=discord.Color.blue())
    embed.set_author(name=scope_label)

    if d["games_together"] == 0:
        embed.description = "They haven't been seated in a hand together yet in this scope."
        return embed

    net_a = d["points_a_from_b"] - d["points_b_from_a"]
    sign = "+" if net_a >= 0 else ""
    lead = f"{name_a} leads" if net_a > 0 else (f"{name_b} leads" if net_a < 0 else "Dead even")
    embed.description = f"**{lead}** · net {sign}{net_a} pts"

    draws_part = f" ({d['draws_together']} draws)" if d["draws_together"] else ""
    embed.add_field(name="Hands together", value=f"{d['games_together']}{draws_part}", inline=True)

    decisive = d["games_together"] - d["draws_together"]
    rate_a = round(d["wins_a"] / decisive * 100) if decisive else 0
    rate_b = round(d["wins_b"] / decisive * 100) if decisive else 0
    embed.add_field(name=f"{name_a} wins", value=f"{d['wins_a']} ({rate_a}%)", inline=True)
    embed.add_field(name=f"{name_b} wins", value=f"{d['wins_b']} ({rate_b}%)", inline=True)

    embed.add_field(
        name=f"{name_a} won directly off {name_b}",
        value=f"{d['points_a_from_b']} pts ({d['times_b_fed_a']}x)",
        inline=True,
    )
    embed.add_field(
        name=f"{name_b} won directly off {name_a}",
        value=f"{d['points_b_from_a']} pts ({d['times_a_fed_b']}x)",
        inline=True,
    )
    embed.add_field(name="\u200b", value="\u200b", inline=True)  # spacer for 3-col layout

    embed.add_field(name=f"{name_a}'s biggest hand vs {name_b}", value=f"+{d['biggest_win_a']}", inline=True)
    embed.add_field(name=f"{name_b}'s biggest hand vs {name_a}", value=f"+{d['biggest_win_b']}", inline=True)

    return embed


async def build_head_to_head_embeds(id_a, name_a, id_b, name_b):
    """Returns a list of 2 embeds: current season, then lifetime."""
    season = await db.get_active_season()
    season_label = f"Season {season['number']} ({season['name']})" if season else "Current Season"
    season_id = season["id"] if season else None

    return [
        await _build_single_h2h_embed(id_a, name_a, id_b, name_b, season_id, season_label),
        await _build_single_h2h_embed(id_a, name_a, id_b, name_b, None, "Lifetime"),
    ]


def _compute_playstyle_scores(stats):
    hands = stats.get("hands", 0)
    wins = stats.get("wins", 0)

    if wins:
        total_faan_weight = sum(f * c for f, c in stats["faan_dist_wins"])
        win_count_check = sum(c for _, c in stats["faan_dist_wins"])
        avg_faan = (total_faan_weight / win_count_check) if win_count_check else 0
        avg_win_points = stats["total_win_points"] / wins
    else:
        avg_faan = 0
        avg_win_points = 0

    discard_losses = stats["losses_by_type"]["discard"]
    defense = (1 - (discard_losses / hands)) * 100 if hands else 0
    attack = min(100, avg_win_points)  # capped at 100 pts/win
    aggression = max(0, min(100, (avg_faan - 3) / (13 - 3) * 100)) if avg_faan else 0
    consistency = (wins / hands * 100) if hands else 0

    return {"attack": attack, "defense": defense, "aggression": aggression, "consistency": consistency}


def _render_playstyle_chart(display_name, scope_label, scores):
    labels = ["Attack", "Defense", "Aggression", "Consistency"]
    values = [scores["attack"], scores["defense"], scores["aggression"], scores["consistency"]]

    angles = np.linspace(0, 2 * np.pi, len(labels), endpoint=False).tolist()
    values_closed = values + values[:1]
    angles_closed = angles + angles[:1]

    fig, ax = plt.subplots(figsize=(4, 4), subplot_kw=dict(polar=True))
    ax.set_facecolor("#faf6ee")
    fig.patch.set_facecolor("#faf6ee")

    ax.plot(angles_closed, values_closed, color="#5b8fd6", linewidth=2)
    ax.fill(angles_closed, values_closed, color="#5b8fd6", alpha=0.4)

    ax.set_xticks(angles)
    ax.set_xticklabels(labels, fontsize=11, color="#444444")
    ax.set_ylim(0, 100)
    ax.set_yticks([25, 50, 75, 100])
    ax.set_yticklabels([])
    ax.grid(color="#dddddd")
    ax.spines["polar"].set_visible(False)

    plt.title(f"Play Style — {display_name}\n{scope_label}", fontsize=12, color="#333333", pad=20)
    plt.tight_layout()

    buf = io.BytesIO()
    plt.savefig(buf, format="png", dpi=150, facecolor=fig.get_facecolor())
    plt.close(fig)  # release matplotlib's internal figure state -- avoids a memory leak across repeated calls
    buf.seek(0)
    return buf


async def build_playstyle_results(discord_id: str, display_name: str):
    """Returns a list of (embed, file_tuple_or_None) pairs -- one per scope
    (current season, lifetime). file_tuple is (filename, BytesIO) when that
    scope has enough hands to show a chart, else None."""
    season = await db.get_active_season()
    scopes = []
    if season:
        scopes.append((f"Season {season['number']} ({season['name']})", season["id"]))
    scopes.append(("Lifetime", None))

    results = []
    for label, season_id in scopes:
        stats = await db.get_player_full_stats(discord_id, season_id)
        hands = stats.get("hands", 0) if stats else 0

        embed = discord.Embed(title=f"🎯 Play Style — {display_name}", color=discord.Color.blue())
        embed.set_author(name=label)

        if hands < PLAYSTYLE_MIN_HANDS:
            embed.description = f"Needs at least {PLAYSTYLE_MIN_HANDS} hands to show ({hands}/{PLAYSTYLE_MIN_HANDS} so far)."
            results.append((embed, None))
            continue

        scores = _compute_playstyle_scores(stats)
        buf = _render_playstyle_chart(display_name, label, scores)
        filename = f"playstyle_{discord_id}_{season_id or 'lifetime'}.png"
        embed.set_image(url=f"attachment://{filename}")
        embed.add_field(name="Attack", value=f"{scores['attack']:.0f}", inline=True)
        embed.add_field(name="Defense", value=f"{scores['defense']:.0f}", inline=True)
        embed.add_field(name="Aggression", value=f"{scores['aggression']:.0f}", inline=True)
        embed.add_field(name="Consistency", value=f"{scores['consistency']:.0f}", inline=True)
        results.append((embed, (filename, buf)))

    return results


async def update_live_leaderboard(client: discord.Client):
    """Best-effort refresh of the pinned live season leaderboard message,
    if one has been set up via /setup-leaderboard. Silently does nothing
    if not configured, and swallows errors so a leaderboard problem never
    breaks game logging."""
    channel_id = await db.get_setting("leaderboard_channel_id")
    message_id = await db.get_setting("leaderboard_message_id")
    if channel_id and message_id:
        try:
            channel = client.get_channel(int(channel_id)) or await client.fetch_channel(int(channel_id))
            message = await channel.fetch_message(int(message_id))
            embed = await build_leaderboard_embed()
            await message.edit(embed=embed)
        except Exception:
            pass


async def update_live_lifetime_leaderboard(client: discord.Client):
    """Same idea as update_live_leaderboard, but for the lifetime board
    set up via /setup-leaderboard-lifetime."""
    channel_id = await db.get_setting("lifetime_leaderboard_channel_id")
    message_id = await db.get_setting("lifetime_leaderboard_message_id")
    if channel_id and message_id:
        try:
            channel = client.get_channel(int(channel_id)) or await client.fetch_channel(int(channel_id))
            message = await channel.fetch_message(int(message_id))
            embed = await build_leaderboard_embed(lifetime=True)
            await message.edit(embed=embed)
        except Exception:
            pass


async def update_live_hall_of_fame(client: discord.Client):
    """Same idea, for the Hall of Fame board set up via /setup-hall-of-fame."""
    channel_id = await db.get_setting("hof_channel_id")
    message_id = await db.get_setting("hof_message_id")
    if channel_id and message_id:
        try:
            channel = client.get_channel(int(channel_id)) or await client.fetch_channel(int(channel_id))
            message = await channel.fetch_message(int(message_id))
            embed = await build_hall_of_fame_embed()
            await message.edit(embed=embed)
        except Exception:
            pass


async def refresh_boards(client: discord.Client):
    """Refreshes every live board that's been set up: season leaderboard,
    lifetime leaderboard, and hall of fame. Call this anywhere a game is
    logged, edited, or deleted, or a season starts/ends."""
    await update_live_leaderboard(client)
    await update_live_lifetime_leaderboard(client)
    await update_live_hall_of_fame(client)


# ---------------------------------------------------------------------------
# Player stat cards
# ---------------------------------------------------------------------------

def _faan_bar_block(dist):
    """dist: list of (faan, count) tuples. One block character per count,
    no scaling -- a count of 16 is literally 16 characters wide."""
    if not dist:
        return "```\n(none yet)\n```"
    lines = []
    for faan, count in dist:
        bar = "█" * count
        lines.append(f"{faan:>2} faan │ {bar} {count}")
    return "```\n" + "\n".join(lines) + "\n```"


def _rank_list(pairs):
    if not pairs:
        return "_none yet_"
    return "\n".join(f"{i+1}. **{name}** — {points} pts" for i, (name, points) in enumerate(pairs))


def _build_single_card_embed(stats: dict, scope_label: str):
    name = stats["display_name"]
    embed = discord.Embed(title=f"🀄 Player Card — {name}", color=discord.Color.blue())
    embed.set_author(name=scope_label)

    if stats.get("hands", 0) == 0:
        embed.description = "No games logged yet in this scope."
        return embed

    rank_str = f"Rank #{stats['rank']}" if stats.get("rank") else "Unranked"
    embed.description = f"**{rank_str}** · {stats['total_points']} total points"

    embed.add_field(name="Hands", value=str(stats["hands"]), inline=True)
    embed.add_field(name="Wins", value=str(stats["wins"]), inline=True)
    embed.add_field(name="Win rate", value=f"{stats['win_rate']:.0f}%", inline=True)

    embed.add_field(name="Avg / hand", value=f"{stats['avg_per_hand']:.1f}", inline=True)
    embed.add_field(name="Biggest win", value=f"+{stats['biggest_win']}", inline=True)
    embed.add_field(name="Biggest loss", value=str(stats["biggest_loss"]), inline=True)

    embed.add_field(name="Net discard given", value=str(stats["net_discard_given"]), inline=True)
    embed.add_field(name="Draws", value=str(stats["draws"]), inline=True)
    embed.add_field(name="False wins", value=str(stats["false_wins"]), inline=True)

    wbt = stats["wins_by_type"]
    lbt = stats["losses_by_type"]
    embed.add_field(
        name="🏆 Wins",
        value=f"Discard: **{wbt['discard']}**\nSelf-draw: **{wbt['self_draw']}**",
        inline=True,
    )
    embed.add_field(
        name="💸 Losses",
        value=f"Discard: **{lbt['discard']}**\nSelf-draw: **{lbt['self_draw']}**",
        inline=True,
    )
    embed.add_field(name="\u200b", value="\u200b", inline=True)  # spacer for 3-column layout

    embed.add_field(name="Faan distribution (wins)", value=_faan_bar_block(stats["faan_dist_wins"]), inline=False)
    embed.add_field(name="Faan distribution (losses)", value=_faan_bar_block(stats["faan_dist_losses"]), inline=False)

    embed.add_field(name="🍚 Fed most by", value=_rank_list(stats["fed_by"]), inline=True)
    embed.add_field(name="🎯 Fed the most to", value=_rank_list(stats["fed_to"]), inline=True)

    return embed


async def build_player_card_embeds(discord_id: str, fallback_name: str):
    """Returns a list of 1-2 embeds: current season, then lifetime."""
    season = await db.get_active_season()
    season_stats = await db.get_player_full_stats(discord_id, season["id"] if season else None)
    lifetime_stats = await db.get_player_full_stats(discord_id, None)

    if season_stats is None and lifetime_stats is None:
        embed = discord.Embed(
            title=f"🀄 Player Card — {fallback_name}",
            description="No games logged yet.",
            color=discord.Color.blue(),
        )
        return [embed]

    season_stats = season_stats or {"display_name": fallback_name, "hands": 0}
    lifetime_stats = lifetime_stats or {"display_name": fallback_name, "hands": 0}
    if season:
        season_label = f"Current Season: Season {season['number']} ({season['name']})"
    else:
        season_label = "Current Season"

    embeds = [
        _build_single_card_embed(season_stats, season_label),
        _build_single_card_embed(lifetime_stats, "Lifetime"),
    ]
    return embeds
