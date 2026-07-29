"""
Shared game-logging logic used by both the /log-game slash command and the
button + modal UI, so scoring/validation lives in exactly one place.
"""

import datetime
import discord

import database as db
import scoring


async def perform_log_game(
    seated,
    win_type: str,
    winner=None,
    faan=None,
    discarder=None,
    false_win_caller=None,
    notes: str = None,
    logged_by_id: str = None,
):
    """seated: list of 4 discord.Member (or User) objects.
    Returns {"ok": True, "game_id": int, "embed": discord.Embed}
    or {"ok": False, "error": str}."""

    if len(set(m.id for m in seated)) != 4:
        return {"ok": False, "error": "All 4 seats must be different players."}

    # Blacklist check
    for m in seated:
        if await db.is_blacklisted(str(m.id)):
            return {
                "ok": False,
                "error": f"{m.display_name} is blacklisted from logging games. "
                "Ask a mod to `/unblacklist` them first.",
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

    # Register/refresh all 4 players
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

    game_id = await db.record_game(
        win_type=win_type,
        faan=faan if win_type in ("discard", "self_draw") else None,
        winner_player_id=winner_id,
        discarder_player_id=discarder_id,
        logged_by=logged_by_id or "unknown",
        score_deltas=deltas,
        notes=notes,
    )

    win_type_labels = {
        "discard": "Discard win",
        "self_draw": "Self-draw win",
        "false_win": "False win",
        "draw": "Draw (void hand)",
    }

    embed = discord.Embed(
        title=f"Game #{game_id} logged",
        color=discord.Color.green(),
        timestamp=datetime.datetime.now(),
    )
    embed.add_field(name="Seats", value=", ".join(m.display_name for m in seated), inline=False)
    embed.add_field(name="Result", value=win_type_labels[win_type], inline=True)
    if faan is not None:
        embed.add_field(name="Faan", value=str(faan), inline=True)

    lines = []
    for m in seated:
        pid = player_ids[m.id]
        d = deltas[pid]
        sign = "+" if d >= 0 else ""
        lines.append(f"{m.display_name}: {sign}{d}")
    embed.add_field(name="Points", value="\n".join(lines), inline=False)

    return {"ok": True, "game_id": game_id, "embed": embed}


async def build_leaderboard_embed():
    rows = await db.get_leaderboard()
    embed = discord.Embed(title="🏆 Mahjong Club Leaderboard", color=discord.Color.gold())
    if not rows:
        embed.description = "No games logged yet. 🀄"
        return embed
    lines = []
    for i, (name, total, games_played) in enumerate(rows, start=1):
        medal = {1: "🥇", 2: "🥈", 3: "🥉"}.get(i, f"{i}.")
        lines.append(f"{medal} **{name}** — {total} pts ({games_played} games)")
    embed.description = "\n".join(lines)
    embed.set_footer(text="Updates automatically after every logged game")
    embed.timestamp = datetime.datetime.now()
    return embed


async def update_live_leaderboard(client: discord.Client):
    """Best-effort refresh of the pinned live leaderboard message, if one
    has been set up via /setup-leaderboard. Silently does nothing if not
    configured, and swallows errors (e.g. message/channel deleted) so a
    leaderboard problem never breaks game logging."""
    channel_id = await db.get_setting("leaderboard_channel_id")
    message_id = await db.get_setting("leaderboard_message_id")
    if not channel_id or not message_id:
        return

    try:
        channel = client.get_channel(int(channel_id)) or await client.fetch_channel(int(channel_id))
        message = await channel.fetch_message(int(message_id))
        embed = await build_leaderboard_embed()
        await message.edit(embed=embed)
    except Exception:
        # Message/channel may have been deleted, or permissions changed.
        # Don't let this break the actual game-logging flow.
        pass
