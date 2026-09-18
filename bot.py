import os
import datetime
from zoneinfo import ZoneInfo
import aiohttp
import discord
from discord import app_commands
from discord.ext import tasks
from dotenv import load_dotenv

import database as db
import game_actions
from views import LogGameButtonView, ModToolsView, _make_date_notes_modal_for_new

load_dotenv()
TOKEN = os.getenv("DISCORD_BOT_TOKEN")
BACKUP_WEBHOOK_URL = os.getenv("BACKUP_WEBHOOK_URL")  # optional -- personal-server backup channel

EASTERN = ZoneInfo("America/New_York")
intents = discord.Intents.default()


class MahjongBot(discord.Client):
    def __init__(self):
        super().__init__(intents=intents)
        self.tree = app_commands.CommandTree(self)
        self.session = None  # aiohttp session, used for delivering webhook backups

    async def setup_hook(self):
        await db.init_db()
        self.session = aiohttp.ClientSession()
        # Re-register persistent views so their buttons keep working after
        # a restart/redeploy (Discord remembers the message, the bot needs
        # to remember which view/custom_ids it maps to).
        self.add_view(LogGameButtonView())
        self.add_view(ModToolsView())
        await self.tree.sync()
        if BACKUP_WEBHOOK_URL:
            weekly_backup_task.start(self)

    async def close(self):
        if self.session:
            await self.session.close()
        await super().close()


bot = MahjongBot()


@tasks.loop(time=datetime.time(hour=9, minute=0, tzinfo=EASTERN))
async def weekly_backup_task(bot_instance):
    """Runs a daily check, but only actually sends on Sundays -- avoids the
    schedule drifting on every redeploy, which a plain 'every N hours'
    interval would be prone to."""
    if datetime.datetime.now(EASTERN).weekday() != 6:  # 6 = Sunday
        return
    if not BACKUP_WEBHOOK_URL:
        return

    filename, file_bytes = await game_actions.build_export_csv()
    if filename is None:
        return  # nothing logged yet -- nothing to back up

    try:
        webhook = discord.Webhook.from_url(BACKUP_WEBHOOK_URL, session=bot_instance.session)
        await webhook.send(
            content=f"📦 Weekly backup — {datetime.date.today().isoformat()}",
            file=discord.File(file_bytes, filename=filename),
            username="Mahjong Bot Backups",
        )
    except Exception as e:
        print(f"Weekly backup failed: {e}")


@bot.event
async def on_ready():
    print(f"Logged in as {bot.user} (id: {bot.user.id})")


@bot.tree.command(name="ping", description="Check if the bot is alive")
async def ping(interaction: discord.Interaction):
    await interaction.response.send_message("Pong! 🀄")


# ---------------------------------------------------------------------------
# Setup commands (mod only)
# ---------------------------------------------------------------------------

@bot.tree.command(name="setup-loggame", description="Post the persistent Log Game button in this channel")
@app_commands.default_permissions(manage_guild=True)
async def setup_loggame(interaction: discord.Interaction):
    embed = discord.Embed(
        title=f"🀄 {game_actions.CLUB_NAME}",
        description="Click below to log a completed hand — no slash command needed.",
        color=discord.Color.blurple(),
    )
    await interaction.response.send_message(embed=embed, view=LogGameButtonView())


@bot.tree.command(name="setup-gamelog", description="Set this channel as the game history log (where logged-game cards get posted)")
@app_commands.default_permissions(manage_guild=True)
async def setup_gamelog(interaction: discord.Interaction):
    await db.set_setting("gamelog_channel_id", str(interaction.channel_id))
    await interaction.response.send_message(
        f"✅ Logged games will now be posted in <#{interaction.channel_id}>."
    )


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


@bot.tree.command(name="setup-leaderboard-lifetime", description="Post a live-updating lifetime leaderboard (all seasons combined) in this channel")
@app_commands.default_permissions(manage_guild=True)
async def setup_leaderboard_lifetime(interaction: discord.Interaction):
    embed = await game_actions.build_leaderboard_embed(lifetime=True)
    await interaction.response.send_message(embed=embed)
    message = await interaction.original_response()
    await db.set_setting("lifetime_leaderboard_channel_id", str(interaction.channel_id))
    await db.set_setting("lifetime_leaderboard_message_id", str(message.id))
    await interaction.followup.send(
        "This message will now auto-update after every logged game, across all seasons.", ephemeral=True
    )


@bot.tree.command(name="setup-hall-of-fame", description="Post a live-updating Hall of Fame (top 10 from every completed season) in this channel")
@app_commands.default_permissions(manage_guild=True)
async def setup_hall_of_fame(interaction: discord.Interaction):
    embed = await game_actions.build_hall_of_fame_embed()
    await interaction.response.send_message(embed=embed)
    message = await interaction.original_response()
    await db.set_setting("hof_channel_id", str(interaction.channel_id))
    await db.set_setting("hof_message_id", str(message.id))
    await interaction.followup.send(
        "This message will now auto-update whenever a season ends or past results change.", ephemeral=True
    )


@bot.tree.command(name="setup-season-dashboard", description="Post a live-updating season-wide stats dashboard in this channel")
@app_commands.default_permissions(manage_guild=True)
async def setup_season_dashboard(interaction: discord.Interaction):
    embed = await game_actions.build_season_dashboard_embed()
    await interaction.response.send_message(embed=embed)
    message = await interaction.original_response()
    await db.set_setting("dashboard_channel_id", str(interaction.channel_id))
    await db.set_setting("dashboard_message_id", str(message.id))
    await interaction.followup.send(
        "This message will now auto-update after every logged game.", ephemeral=True
    )


@bot.tree.command(name="setup-modtools", description="Post the mod tools panel in this channel")
@app_commands.default_permissions(manage_guild=True)
async def setup_modtools(interaction: discord.Interaction):
    embed = discord.Embed(
        title="🛠️ Mod Tools",
        description=(
            "**Delete Game** — remove a logged game and reverse its points\n"
            "**Edit Game** — correct a mis-entered faan count\n"
            "**Blacklist / Unblacklist** — control who can be logged in games\n"
            "**View Blacklist** — see who's currently blacklisted\n"
            "**New Season** — archive the current leaderboard and start fresh\n"
            "**Reset Season** — ⚠️ pick any season (including past ones) and permanently delete its games (type-to-confirm required)\n"
            "**Export CSV** — download all logged games for Power BI / Tableau / Excel"
        ),
        color=discord.Color.dark_gold(),
    )
    await interaction.response.send_message(embed=embed, view=ModToolsView())


# ---------------------------------------------------------------------------
# Logging (also reachable via the persistent button posted by /setup-loggame)
# ---------------------------------------------------------------------------

@bot.tree.command(name="log-game", description="Log a completed mahjong hand")
async def log_game(interaction: discord.Interaction):
    await interaction.response.send_modal(_make_date_notes_modal_for_new())


# ---------------------------------------------------------------------------
# Read-only commands
# ---------------------------------------------------------------------------

@bot.tree.command(name="leaderboard", description="Show the current season's leaderboard (or a past season's)")
@app_commands.describe(season_number="View a specific past season instead of the current one")
async def leaderboard(interaction: discord.Interaction, season_number: int = None):
    if season_number is not None:
        season = await db.get_season_by_number(season_number)
        if not season:
            await interaction.response.send_message(f"No season #{season_number} found. Try `/season-list`.", ephemeral=True)
            return
        embed = await game_actions.build_leaderboard_embed(season)
    else:
        embed = await game_actions.build_leaderboard_embed()
    await interaction.response.send_message(embed=embed)


@bot.tree.command(name="leaderboard-lifetime", description="Show the all-time leaderboard across every season")
async def leaderboard_lifetime(interaction: discord.Interaction):
    embed = await game_actions.build_leaderboard_embed(lifetime=True)
    await interaction.response.send_message(embed=embed)


@bot.tree.command(name="stats", description="Show a player's stat card (current season + lifetime)")
@app_commands.describe(player="Player to look up (defaults to you)")
async def stats(interaction: discord.Interaction, player: discord.Member = None):
    member = player or interaction.user
    embeds = await game_actions.build_player_card_embeds(str(member.id), member.display_name)
    await interaction.response.send_message(embeds=embeds)


@bot.tree.command(name="play-style", description="Show a player's play style radar chart (Attack/Defense/Aggression/Consistency)")
@app_commands.describe(player="Player to look up (defaults to you)")
async def play_style(interaction: discord.Interaction, player: discord.Member = None):
    member = player or interaction.user
    await interaction.response.defer()
    results = await game_actions.build_playstyle_results(str(member.id), member.display_name)
    embeds = [r[0] for r in results]
    files = [discord.File(r[1][1], filename=r[1][0]) for r in results if r[1] is not None]
    await interaction.followup.send(embeds=embeds, files=files)


@bot.tree.command(name="head-to-head", description="Compare two players' record against each other")
@app_commands.describe(player_a="First player", player_b="Second player")
async def head_to_head(interaction: discord.Interaction, player_a: discord.Member, player_b: discord.Member):
    if player_a.id == player_b.id:
        await interaction.response.send_message("Pick two different players.", ephemeral=True)
        return
    embeds = await game_actions.build_head_to_head_embeds(
        str(player_a.id), player_a.display_name, str(player_b.id), player_b.display_name
    )
    await interaction.response.send_message(embeds=embeds)


@bot.tree.command(name="game-history", description="Show a player's game history for the current season")
@app_commands.describe(player="Player to look up (defaults to you)")
async def game_history(interaction: discord.Interaction, player: discord.Member = None):
    member = player or interaction.user
    embed = await game_actions.build_player_history_embed(str(member.id), member.display_name)
    await interaction.response.send_message(embed=embed)


@bot.tree.command(name="recent-games", description="Show the most recently logged games")
async def recent_games(interaction: discord.Interaction):
    rows = await db.get_recent_games(10)
    if not rows:
        await interaction.response.send_message("No games logged yet.")
        return

    lines = []
    for game_id, ts, win_type, faan, winner_name, discarder_name, season_number, season_game_number in rows:
        dt = datetime.datetime.fromtimestamp(ts).strftime("%b %d, %I:%M %p")
        hand_ref = f"S{season_number}-H{season_game_number}" if season_number is not None else f"H{game_id}"
        if win_type == "discard":
            desc = f"{winner_name} won off {discarder_name}'s discard ({faan} faan)"
        elif win_type == "self_draw":
            desc = f"{winner_name} self-drew ({faan} faan)"
        elif win_type == "false_win":
            desc = f"{winner_name} called a false win"
        else:
            desc = "Draw / void hand"
        lines.append(f"**{hand_ref}** ({dt}) — {desc}")

    embed = discord.Embed(
        title="🀄 Recent Games", description="\n".join(lines), color=discord.Color.purple()
    )
    await interaction.response.send_message(embed=embed)


@bot.tree.command(name="season-info", description="Show the current active season")
async def season_info(interaction: discord.Interaction):
    season = await db.get_active_season()
    if not season:
        await interaction.response.send_message("No active season set.", ephemeral=True)
        return
    await interaction.response.send_message(f"📅 Current season: **Season {season['number']} ({season['name']})**")


@bot.tree.command(name="season-list", description="List every season this club has had")
async def season_list(interaction: discord.Interaction):
    embed = await game_actions.build_season_list_embed()
    await interaction.response.send_message(embed=embed)


@bot.tree.command(name="hall-of-fame", description="Show the top 3 finishers from every season")
async def hall_of_fame(interaction: discord.Interaction):
    embed = await game_actions.build_hall_of_fame_embed()
    await interaction.response.send_message(embed=embed)


@bot.tree.command(name="season-dashboard", description="Show season-wide stats (hands, players, faan distribution, etc.)")
async def season_dashboard(interaction: discord.Interaction):
    embed = await game_actions.build_season_dashboard_embed()
    await interaction.response.send_message(embed=embed)


if __name__ == "__main__":
    if not TOKEN:
        raise RuntimeError(
            "DISCORD_BOT_TOKEN not set. Copy .env.example to .env and fill in your bot token."
        )
    bot.run(TOKEN)
