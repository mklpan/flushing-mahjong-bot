import os
import datetime
import discord
from discord import app_commands
from dotenv import load_dotenv

import database as db
import scoring

load_dotenv()
TOKEN = os.getenv("DISCORD_BOT_TOKEN")

intents = discord.Intents.default()


class MahjongBot(discord.Client):
    def __init__(self):
        super().__init__(intents=intents)
        self.tree = app_commands.CommandTree(self)

    async def setup_hook(self):
        await db.init_db()
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
    seated = [player1, player2, player3, player4]
    if len(set(m.id for m in seated)) != 4:
        await interaction.response.send_message(
            "All 4 seats must be different players.", ephemeral=True
        )
        return

    wt = win_type.value

    try:
        # Validate inputs per win type
        if wt in ("discard", "self_draw"):
            if winner is None or faan is None:
                await interaction.response.send_message(
                    "Discard and self-draw wins require both `winner` and `faan`.",
                    ephemeral=True,
                )
                return
            if winner not in seated:
                await interaction.response.send_message(
                    "Winner must be one of the 4 seated players.", ephemeral=True
                )
                return
            scoring.validate_win_faan(faan)
            faan = scoring.clamp_faan(faan)

        if wt == "discard":
            if discarder is None:
                await interaction.response.send_message(
                    "Discard wins require `discarder`.", ephemeral=True
                )
                return
            if discarder not in seated or discarder == winner:
                await interaction.response.send_message(
                    "Discarder must be one of the other 3 seated players.",
                    ephemeral=True,
                )
                return

        if wt == "false_win":
            if false_win_caller is None or false_win_caller not in seated:
                await interaction.response.send_message(
                    "False wins require `false_win_caller` to be one of the 4 seated players.",
                    ephemeral=True,
                )
                return

    except scoring.ScoringError as e:
        await interaction.response.send_message(str(e), ephemeral=True)
        return

    # Register/refresh all 4 players in DB
    player_ids = {}
    for m in seated:
        player_ids[m.id] = await db.get_or_create_player(str(m.id), m.display_name)

    # Build score deltas for all 4 seated players (default 0)
    deltas = {player_ids[m.id]: 0 for m in seated}
    winner_id = discarder_id = None

    if wt == "discard":
        result = scoring.score_discard_win(faan)
        winner_id = player_ids[winner.id]
        discarder_id = player_ids[discarder.id]
        deltas[winner_id] += result["winner"]
        deltas[discarder_id] += result["discarder"]

    elif wt == "self_draw":
        result = scoring.score_self_draw_win(faan)
        winner_id = player_ids[winner.id]
        deltas[winner_id] += result["winner"]
        for m in seated:
            if m.id != winner.id:
                deltas[player_ids[m.id]] += result["each_opponent"]

    elif wt == "false_win":
        result = scoring.score_false_win()
        winner_id = player_ids[false_win_caller.id]  # stored as "winner" for the false-win row
        deltas[winner_id] += result["false_winner"]
        for m in seated:
            if m.id != false_win_caller.id:
                deltas[player_ids[m.id]] += result["each_opponent"]

    elif wt == "draw":
        pass  # all deltas stay 0

    game_id = await db.record_game(
        win_type=wt,
        faan=faan if wt in ("discard", "self_draw") else None,
        winner_player_id=winner_id,
        discarder_player_id=discarder_id,
        logged_by=str(interaction.user.id),
        score_deltas=deltas,
        notes=notes,
    )

    # Build a friendly confirmation embed
    embed = discord.Embed(
        title=f"Game #{game_id} logged",
        color=discord.Color.green(),
        timestamp=datetime.datetime.now(),
    )
    embed.add_field(name="Seats", value=", ".join(m.display_name for m in seated), inline=False)
    embed.add_field(name="Result", value=win_type.name, inline=True)
    if faan is not None:
        embed.add_field(name="Faan", value=str(faan), inline=True)

    lines = []
    for m in seated:
        pid = player_ids[m.id]
        d = deltas[pid]
        sign = "+" if d >= 0 else ""
        lines.append(f"{m.display_name}: {sign}{d}")
    embed.add_field(name="Points", value="\n".join(lines), inline=False)

    await interaction.response.send_message(embed=embed)


@bot.tree.command(name="leaderboard", description="Show the club leaderboard")
async def leaderboard(interaction: discord.Interaction):
    rows = await db.get_leaderboard()
    if not rows:
        await interaction.response.send_message("No games logged yet. 🀄")
        return

    embed = discord.Embed(title="🏆 Mahjong Club Leaderboard", color=discord.Color.gold())
    lines = []
    for i, (name, total, games_played) in enumerate(rows, start=1):
        medal = {1: "🥇", 2: "🥈", 3: "🥉"}.get(i, f"{i}.")
        lines.append(f"{medal} **{name}** — {total} pts ({games_played} games)")
    embed.description = "\n".join(lines)
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


if __name__ == "__main__":
    if not TOKEN:
        raise RuntimeError(
            "DISCORD_BOT_TOKEN not set. Copy .env.example to .env and fill in your bot token."
        )
    bot.run(TOKEN)
