import csv
import io
import os
import datetime
import discord
from discord import app_commands
from dotenv import load_dotenv

import database as db
import scoring
import game_actions
from views import LogGameButtonView

load_dotenv()
TOKEN = os.getenv("DISCORD_BOT_TOKEN")

intents = discord.Intents.default()


class MahjongBot(discord.Client):
    def __init__(self):
        super().__init__(intents=intents)
        self.tree = app_commands.CommandTree(self)

    async def setup_hook(self):
        await db.init_db()
        # Re-register the persistent Log Game button so it keeps working
        # after a restart/redeploy (Discord remembers the message, the
        # bot needs to remember the view).
        self.add_view(LogGameButtonView())
        await self.tree.sync()


bot = MahjongBot()

WIN_TYPE_CHOICES = [
    app_commands.Choice(name="Discard win", value="discard"),
    app_commands.Choice(name="Self-draw win", value="self_draw"),
    app_commands.Choice(name="False win", value="false_win"),
    app_commands.Choice(name="Draw (void hand)", value="draw"),
]


@bot.event
async def on_ready():
    print(f"Logged in as {bot.user} (id: {bot.user.id})")


@bot.tree.command(name="ping", description="Check if the bot is alive")
async def ping(interaction: discord.Interaction):
    await interaction.response.send_message("Pong! 🀄")


# ---------------------------------------------------------------------------
# Setup commands (mod only)
# ---------------------------------------------------------------------------

@bot.tree.command(name="setup", description="Post the persistent Log Game button in this channel")
@app_commands.default_permissions(manage_guild=True)
async def setup(interaction: discord.Interaction):
    embed = discord.Embed(
        title="🀄 Mahjong Club",
        description="Click below to log a completed hand — no slash command needed.",
        color=discord.Color.blurple(),
    )
    await interaction.response.send_message(embed=embed, view=LogGameButtonView())


@bot.tree.command(name="setup-leaderboard", description="Post a live-updating leaderboard in this channel")
@app_commands.default_permissions(manage_guild=True)
async def setup_leaderboard(interaction: discord.Interaction):
    embed = await game_actions.build_leaderboard_embed()
    await interaction.response.send_message(embed=embed)
    message = await interaction.original_response()
    await db.set_setting("leaderboard_channel_id", str(interaction.channel_id))
    await db.set_setting("leaderboard_message_id", str(message.id))
    await interaction.followup.send(
        "This message will now auto-update after every logged game.", ephemeral=True
    )


# ---------------------------------------------------------------------------
# Logging (slash command version -- the button/modal flow lives in views.py)
# ---------------------------------------------------------------------------

@bot.tree.command(name="log-game", description="Log a completed mahjong hand")
@app_commands.describe(
    player1="Seat 1",
    player2="Seat 2",
    player3="Seat 3",
    player4="Seat 4",
    win_type="How the hand ended",
    winner="Who won (required for discard / self-draw wins)",
    faan="Faan count (required for discard / self-draw wins, 3-13)",
    discarder="Who discarded the winning tile (required for discard wins)",
    false_win_caller="Who made the invalid win call (required for false wins)",
    notes="Optional notes about the hand",
)
@app_commands.choices(win_type=WIN_TYPE_CHOICES)
async def log_game(
    interaction: discord.Interaction,
    player1: discord.Member,
    player2: discord.Member,
    player3: discord.Member,
    player4: discord.Member,
    win_type: app_commands.Choice[str],
    winner: discord.Member = None,
    faan: int = None,
    discarder: discord.Member = None,
    false_win_caller: discord.Member = None,
    notes: str = None,
):
    result = await game_actions.perform_log_game(
        seated=[player1, player2, player3, player4],
        win_type=win_type.value,
        winner=winner,
        faan=faan,
        discarder=discarder,
        false_win_caller=false_win_caller,
        notes=notes,
        logged_by_id=str(interaction.user.id),
    )

    if not result["ok"]:
        await interaction.response.send_message(result["error"], ephemeral=True)
        return

    await interaction.response.send_message(embed=result["embed"])
    await game_actions.update_live_leaderboard(interaction.client)


# ---------------------------------------------------------------------------
# Read-only commands
# ---------------------------------------------------------------------------

@bot.tree.command(name="leaderboard", description="Show the club leaderboard")
async def leaderboard(interaction: discord.Interaction):
    embed = await game_actions.build_leaderboard_embed()
    await interaction.response.send_message(embed=embed)


@bot.tree.command(name="stats", description="Show stats for a player")
@app_commands.describe(player="Player to look up (defaults to you)")
async def stats(interaction: discord.Interaction, player: discord.Member = None):
    member = player or interaction.user
    data = await db.get_player_stats(str(member.id))
    if not data:
        await interaction.response.send_message(
            f"{member.display_name} hasn't played any logged games yet.", ephemeral=True
        )
        return

    embed = discord.Embed(title=f"📊 Stats for {data['display_name']}", color=discord.Color.blue())
    embed.add_field(name="Games played", value=str(data["games_played"]))
    embed.add_field(name="Wins", value=str(data["wins"]))
    embed.add_field(name="Win rate", value=f"{data['win_rate']:.1f}%")
    embed.add_field(name="Total points", value=str(data["total_points"]))
    embed.add_field(name="Avg winning faan", value=str(data["avg_faan"] or "—"))
    embed.add_field(name="False wins called", value=str(data["false_wins"]))
    await interaction.response.send_message(embed=embed)


@bot.tree.command(name="recent-games", description="Show the most recently logged games")
async def recent_games(interaction: discord.Interaction):
    rows = await db.get_recent_games(10)
    if not rows:
        await interaction.response.send_message("No games logged yet.")
        return

    lines = []
    for game_id, ts, win_type, faan, winner_name, discarder_name in rows:
        dt = datetime.datetime.fromtimestamp(ts).strftime("%b %d, %I:%M %p")
        if win_type == "discard":
            desc = f"{winner_name} won off {discarder_name}'s discard ({faan} faan)"
        elif win_type == "self_draw":
            desc = f"{winner_name} self-drew ({faan} faan)"
        elif win_type == "false_win":
            desc = f"{winner_name} called a false win"
        else:
            desc = "Draw / void hand"
        lines.append(f"**#{game_id}** ({dt}) — {desc}")

    embed = discord.Embed(
        title="🀄 Recent Games", description="\n".join(lines), color=discord.Color.purple()
    )
    await interaction.response.send_message(embed=embed)


# ---------------------------------------------------------------------------
# Mod tools
# ---------------------------------------------------------------------------

@bot.tree.command(name="delete-game", description="[Mod] Delete a logged game and reverse its points")
@app_commands.describe(game_id="The game # shown in /recent-games or the log confirmation")
@app_commands.default_permissions(manage_guild=True)
async def delete_game(interaction: discord.Interaction, game_id: int):
    game = await db.get_game(game_id)
    if not game:
        await interaction.response.send_message(f"No game found with id #{game_id}.", ephemeral=True)
        return

    await db.delete_game(game_id)
    await game_actions.update_live_leaderboard(interaction.client)

    lines = [f"{name}: {points:+d}" for name, points in game["scores"]]
    await interaction.response.send_message(
        f"🗑️ Deleted game #{game_id} ({game['win_type']}"
        + (f", {game['faan']} faan" if game["faan"] else "")
        + ").\nReversed:\n" + "\n".join(lines)
    )


@bot.tree.command(name="edit-game", description="[Mod] Correct the faan count on a logged discard/self-draw win")
@app_commands.describe(
    game_id="The game # shown in /recent-games or the log confirmation",
    new_faan="The corrected faan count (3-13)",
)
@app_commands.default_permissions(manage_guild=True)
async def edit_game(interaction: discord.Interaction, game_id: int, new_faan: int):
    ids = await db.get_game_player_ids(game_id)
    if not ids:
        await interaction.response.send_message(f"No game found with id #{game_id}.", ephemeral=True)
        return

    if ids["win_type"] not in ("discard", "self_draw"):
        await interaction.response.send_message(
            "Only discard and self-draw wins have a faan count to edit. "
            "For other corrections, delete the game with `/delete-game` and re-log it.",
            ephemeral=True,
        )
        return

    try:
        scoring.validate_win_faan(new_faan)
    except scoring.ScoringError as e:
        await interaction.response.send_message(str(e), ephemeral=True)
        return
    new_faan = scoring.clamp_faan(new_faan)

    new_deltas = {pid: 0 for pid in ids["player_ids"]}
    if ids["win_type"] == "discard":
        result = scoring.score_discard_win(new_faan)
        new_deltas[ids["winner_id"]] += result["winner"]
        new_deltas[ids["discarder_id"]] += result["discarder"]
    else:
        result = scoring.score_self_draw_win(new_faan)
        new_deltas[ids["winner_id"]] += result["winner"]
        for pid in ids["player_ids"]:
            if pid != ids["winner_id"]:
                new_deltas[pid] += result["each_opponent"]

    await db.update_game_faan(game_id, new_faan, new_deltas)
    await game_actions.update_live_leaderboard(interaction.client)

    game = await db.get_game(game_id)
    lines = [f"{name}: {points:+d}" for name, points in game["scores"]]
    await interaction.response.send_message(
        f"✏️ Game #{game_id} updated to {new_faan} faan.\nNew points:\n" + "\n".join(lines)
    )


@bot.tree.command(name="blacklist", description="[Mod] Prevent a player from being logged in future games")
@app_commands.describe(player="Player to blacklist")
@app_commands.default_permissions(manage_guild=True)
async def blacklist(interaction: discord.Interaction, player: discord.Member):
    await db.get_or_create_player(str(player.id), player.display_name)
    await db.set_blacklisted(str(player.id), True)
    await interaction.response.send_message(f"🚫 {player.display_name} has been blacklisted from logging games.")


@bot.tree.command(name="unblacklist", description="[Mod] Remove a player from the blacklist")
@app_commands.describe(player="Player to unblacklist")
@app_commands.default_permissions(manage_guild=True)
async def unblacklist(interaction: discord.Interaction, player: discord.Member):
    updated = await db.set_blacklisted(str(player.id), False)
    if not updated:
        await interaction.response.send_message(
            f"{player.display_name} has no record yet (never logged a game).", ephemeral=True
        )
        return
    await interaction.response.send_message(f"✅ {player.display_name} has been unblacklisted.")


@bot.tree.command(name="blacklist-list", description="[Mod] Show all currently blacklisted players")
@app_commands.default_permissions(manage_guild=True)
async def blacklist_list(interaction: discord.Interaction):
    rows = await db.get_blacklisted_players()
    if not rows:
        await interaction.response.send_message("No one is currently blacklisted.", ephemeral=True)
        return
    lines = [f"- {name}" for name, _ in rows]
    await interaction.response.send_message("**Blacklisted players:**\n" + "\n".join(lines), ephemeral=True)


@bot.tree.command(name="export-csv", description="[Mod] Export all logged games as a CSV file")
@app_commands.default_permissions(manage_guild=True)
async def export_csv(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    rows = await db.export_rows()
    if not rows:
        await interaction.followup.send("No games logged yet.", ephemeral=True)
        return

    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(
        [
            "game_id", "timestamp_utc", "win_type", "faan",
            "player_name", "discord_id", "points", "is_winner", "is_discarder", "notes",
        ]
    )
    for row in rows:
        game_id, ts, win_type, faan, player_name, discord_id, points, is_winner, is_discarder, notes = row
        dt = datetime.datetime.fromtimestamp(ts, tz=datetime.timezone.utc).isoformat()
        writer.writerow([game_id, dt, win_type, faan, player_name, discord_id, points, is_winner, is_discarder, notes])

    buffer.seek(0)
    file_bytes = io.BytesIO(buffer.getvalue().encode("utf-8"))
    filename = f"mahjong_export_{datetime.date.today().isoformat()}.csv"
    await interaction.followup.send(
        content="Here's your export — one row per player per game, ready for Power BI / Tableau / Excel.",
        file=discord.File(file_bytes, filename=filename),
        ephemeral=True,
    )


if __name__ == "__main__":
    if not TOKEN:
        raise RuntimeError(
            "DISCORD_BOT_TOKEN not set. Copy .env.example to .env and fill in your bot token."
        )
    bot.run(TOKEN)
