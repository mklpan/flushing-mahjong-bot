"""
Button + modal UI for the mahjong bot.

Log-game flow:
  1. Persistent "Log Game" button (posted via /setup) -> opens DateNotesModal
  2. DateNotesModal (date, notes) -> shows GameDetailsView
  3. GameDetailsView: faan / win type / 4 seated players (faan dropdown
     dynamically disappears if win type is False Win or Draw) -> Next
  4. RoleSelectView: winner+discarder (discard), winner (self-draw),
     false-win caller (false win), or nothing (draw) -> Submit Hand
  5. On submit: card posted to the game-log channel, live leaderboard
     refreshed, and the ephemeral flow message deletes itself.

Mod tools flow:
  A persistent button panel (posted via /setup-modtools) with buttons for
  delete/edit game, blacklist/unblacklist, view blacklist, new season,
  and CSV export -- restricted to users with the Manage Server permission
  as a code-level backup to the channel itself being mod-only.
"""

import csv
import io
import datetime
import discord

import database as db
import scoring
import game_actions

FAAN_OPTIONS = [discord.SelectOption(label=f"{n} faan", value=str(n)) for n in range(3, 14)]

WIN_TYPE_OPTIONS = [
    discord.SelectOption(label="Discard win", value="discard"),
    discord.SelectOption(label="Self-draw win", value="self_draw"),
    discord.SelectOption(label="False win", value="false_win"),
    discord.SelectOption(label="Draw (void hand)", value="draw"),
]


def _requires_faan(win_type):
    return win_type in ("discard", "self_draw")


def _has_mod_permission(interaction: discord.Interaction) -> bool:
    perms = getattr(interaction.user, "guild_permissions", None)
    return bool(perms and perms.manage_guild)


async def _finish_log_flow(interaction: discord.Interaction, result: dict):
    """Shared tail end for every win-type path: post the card to the
    game-log channel (or fall back to the current channel), refresh the
    live leaderboard, and make the ephemeral flow message disappear."""
    if not result["ok"]:
        await interaction.edit_original_response(content=f"⚠️ {result['error']}", embed=None, view=None)
        return

    posted = await game_actions.post_logged_game(interaction.client, result["embed"])
    if not posted:
        # No game-log channel configured -- post it right here instead.
        await interaction.channel.send(embed=result["embed"])

    await game_actions.update_live_leaderboard(interaction.client)

    # "the logged game card should disappear after submission"
    try:
        await interaction.delete_original_response()
    except Exception:
        await interaction.edit_original_response(content="✅ Logged!", embed=None, view=None)


# ---------------------------------------------------------------------------
# Step 1: persistent button + date/notes modal
# ---------------------------------------------------------------------------

class LogGameButtonView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(
        label="Log Game", emoji="🀄", style=discord.ButtonStyle.primary, custom_id="mahjong_log_game_button"
    )
    async def log_game_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(DateNotesModal())


class DateNotesModal(discord.ui.Modal, title="Log a Hand — Date & Notes"):
    date_input = discord.ui.TextInput(
        label="Date (YYYY-MM-DD, blank = today)", required=False, max_length=10
    )
    notes_input = discord.ui.TextInput(
        label="Notes (optional)", required=False, max_length=200, style=discord.TextStyle.paragraph
    )

    async def on_submit(self, interaction: discord.Interaction):
        raw_date = self.date_input.value.strip()
        if raw_date:
            try:
                datetime.date.fromisoformat(raw_date)
            except ValueError:
                await interaction.response.send_message(
                    "Date must be in YYYY-MM-DD format (e.g. 2026-07-29). Click **Log Game** again to retry.",
                    ephemeral=True,
                )
                return
            game_date = raw_date
        else:
            game_date = datetime.date.today().isoformat()

        view = GameDetailsView(
            game_date=game_date,
            notes=self.notes_input.value.strip() or None,
            requester_id=interaction.user.id,
        )
        await interaction.response.send_message(content=view.status_text(), view=view, ephemeral=True)


# ---------------------------------------------------------------------------
# Step 2: faan / win type / seated players
# ---------------------------------------------------------------------------

class FaanSelect(discord.ui.Select):
    def __init__(self):
        super().__init__(placeholder="Faan (3-13)", options=FAAN_OPTIONS, min_values=1, max_values=1, row=0)

    async def callback(self, interaction: discord.Interaction):
        self.view.faan = int(self.values[0])
        await interaction.response.edit_message(content=self.view.status_text(), view=self.view)


class WinTypeSelect(discord.ui.Select):
    def __init__(self):
        super().__init__(placeholder="Win type", options=WIN_TYPE_OPTIONS, min_values=1, max_values=1, row=1)

    async def callback(self, interaction: discord.Interaction):
        self.view.win_type = self.values[0]
        if not _requires_faan(self.view.win_type):
            self.view.faan = None
        self.view.rebuild_items()
        await interaction.response.edit_message(content=self.view.status_text(), view=self.view)


class SeatedSelect(discord.ui.UserSelect):
    def __init__(self):
        super().__init__(placeholder="Select all 4 seated players", min_values=4, max_values=4, row=2)

    async def callback(self, interaction: discord.Interaction):
        self.view.seated = list(self.values)
        await interaction.response.edit_message(content=self.view.status_text(), view=self.view)


class NextButton(discord.ui.Button):
    def __init__(self):
        super().__init__(label="Next", style=discord.ButtonStyle.primary, row=3)

    async def callback(self, interaction: discord.Interaction):
        v: GameDetailsView = self.view
        if v.seated is None or len(v.seated) != 4:
            await interaction.response.edit_message(content="Please select all 4 seated players first.", view=v)
            return
        if _requires_faan(v.win_type) and v.faan is None:
            await interaction.response.edit_message(content="Please select a faan count.", view=v)
            return
        if v.win_type is None:
            await interaction.response.edit_message(content="Please select a win type.", view=v)
            return

        if v.win_type == "draw":
            result = await game_actions.perform_log_game(
                seated=v.seated,
                win_type="draw",
                notes=v.notes,
                logged_by_id=str(interaction.user.id),
                game_date=v.game_date,
            )
            await interaction.response.edit_message(content="Logging...", view=None)
            await _finish_log_flow(interaction, result)
            return

        role_view = RoleSelectView(parent=v)
        await interaction.response.edit_message(content=role_view.status_text(), view=role_view)


class GameDetailsView(discord.ui.View):
    def __init__(self, game_date: str, notes, requester_id: int):
        super().__init__(timeout=300)
        self.game_date = game_date
        self.notes = notes
        self.requester_id = requester_id
        self.faan = None
        self.win_type = None
        self.seated = None
        self.rebuild_items()

    def rebuild_items(self):
        self.clear_items()
        if _requires_faan(self.win_type) or self.win_type is None:
            self.add_item(FaanSelect())
        self.add_item(WinTypeSelect())
        self.add_item(SeatedSelect())
        self.add_item(NextButton())

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.requester_id:
            await interaction.response.send_message(
                "Only the person who started this /log-game flow can fill it out.", ephemeral=True
            )
            return False
        return True

    def status_text(self) -> str:
        lines = [f"**Logging a hand — {self.game_date}**"]
        if self.notes:
            lines.append(f"Notes: {self.notes}")
        lines.append(f"Faan: {self.faan if self.faan is not None else '_n/a for this win type_' if not _requires_faan(self.win_type) else '_not selected_'}")
        lines.append(f"Win type: {self.win_type or '_not selected_'}")
        lines.append(f"Seated: {', '.join(m.display_name for m in self.seated) if self.seated else '_not selected_'}")
        lines.append("\nFill in the fields above, then press **Next**.")
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# Step 3: role selection (winner / discarder / false-win caller)
# ---------------------------------------------------------------------------

class MemberChoiceSelect(discord.ui.Select):
    def __init__(self, members, placeholder, row, attr_name):
        options = [
            discord.SelectOption(label=m.display_name, value=str(m.id)) for m in members
        ]
        super().__init__(placeholder=placeholder, options=options, min_values=1, max_values=1, row=row)
        self.attr_name = attr_name

    async def callback(self, interaction: discord.Interaction):
        member_id = int(self.values[0])
        member = next(m for m in self.view.parent.seated if m.id == member_id)
        setattr(self.view, self.attr_name, member)
        await interaction.response.edit_message(content=self.view.status_text(), view=self.view)


class RoleSubmitButton(discord.ui.Button):
    def __init__(self):
        super().__init__(label="Submit Hand", style=discord.ButtonStyle.success, row=2)

    async def callback(self, interaction: discord.Interaction):
        v: RoleSelectView = self.view

        # Duplicate-submission guard: check-and-set BEFORE any await, and
        # disable every component immediately so a second click physically
        # can't register on this message.
        if v.submitted:
            return
        v.submitted = True
        for item in v.children:
            item.disabled = True

        p = v.parent
        win_type = p.win_type

        if win_type == "discard" and (v.winner is None or v.discarder is None):
            v.submitted = False
            for item in v.children:
                item.disabled = False
            await interaction.response.edit_message(content="Please select both winner and discarder.", view=v)
            return
        if win_type == "self_draw" and v.winner is None:
            v.submitted = False
            for item in v.children:
                item.disabled = False
            await interaction.response.edit_message(content="Please select the winner.", view=v)
            return
        if win_type == "false_win" and v.false_winner is None:
            v.submitted = False
            for item in v.children:
                item.disabled = False
            await interaction.response.edit_message(content="Please select who made the false win call.", view=v)
            return

        await interaction.response.edit_message(content="Logging...", view=v)

        result = await game_actions.perform_log_game(
            seated=p.seated,
            win_type=win_type,
            winner=v.winner if win_type in ("discard", "self_draw") else None,
            faan=p.faan,
            discarder=v.discarder if win_type == "discard" else None,
            false_win_caller=v.false_winner if win_type == "false_win" else None,
            notes=p.notes,
            logged_by_id=str(interaction.user.id),
            game_date=p.game_date,
        )
        await _finish_log_flow(interaction, result)


class RoleSelectView(discord.ui.View):
    def __init__(self, parent: GameDetailsView):
        super().__init__(timeout=300)
        self.parent = parent
        self.winner = None
        self.discarder = None
        self.false_winner = None
        self.submitted = False

        if parent.win_type == "discard":
            self.add_item(MemberChoiceSelect(parent.seated, "Select the winner", 0, "winner"))
            self.add_item(MemberChoiceSelect(parent.seated, "Select the discarder", 1, "discarder"))
        elif parent.win_type == "self_draw":
            self.add_item(MemberChoiceSelect(parent.seated, "Select the self-draw winner", 0, "winner"))
        elif parent.win_type == "false_win":
            self.add_item(MemberChoiceSelect(parent.seated, "Select who made the false win call", 0, "false_winner"))
        self.add_item(RoleSubmitButton())

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.parent.requester_id:
            await interaction.response.send_message(
                "Only the person who started this /log-game flow can fill it out.", ephemeral=True
            )
            return False
        return True

    def status_text(self) -> str:
        lines = [f"**{self.parent.win_type.replace('_', ' ').title()} — {self.parent.game_date}**"]
        if self.parent.win_type == "discard":
            lines.append(f"Winner: {self.winner.display_name if self.winner else '_not selected_'}")
            lines.append(f"Discarder: {self.discarder.display_name if self.discarder else '_not selected_'}")
        elif self.parent.win_type == "self_draw":
            lines.append(f"Winner: {self.winner.display_name if self.winner else '_not selected_'}")
        elif self.parent.win_type == "false_win":
            lines.append(f"False-win caller: {self.false_winner.display_name if self.false_winner else '_not selected_'}")
        lines.append("\nThen press **Submit Hand**.")
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# Mod tools panel
# ---------------------------------------------------------------------------

class DeleteGameModal(discord.ui.Modal, title="Delete a Game"):
    game_id_input = discord.ui.TextInput(label="Game ID (the # from the logged card)", max_length=10)

    async def on_submit(self, interaction: discord.Interaction):
        if not self.game_id_input.value.strip().isdigit():
            await interaction.response.send_message("Game ID must be a number.", ephemeral=True)
            return
        game_id = int(self.game_id_input.value.strip())
        game = await db.get_game(game_id)
        if not game:
            await interaction.response.send_message(f"No game found with id #{game_id}.", ephemeral=True)
            return
        await db.delete_game(game_id)
        await game_actions.update_live_leaderboard(interaction.client)
        lines = [f"{name}: {points:+d}" for name, points in game["scores"]]
        await interaction.response.send_message(
            f"🗑️ Deleted game #{game_id}. Reversed:\n" + "\n".join(lines), ephemeral=True
        )


class EditGameModal(discord.ui.Modal, title="Edit a Game's Faan"):
    game_id_input = discord.ui.TextInput(label="Game ID", max_length=10)
    new_faan_input = discord.ui.TextInput(label="New faan count (3-13)", max_length=3)

    async def on_submit(self, interaction: discord.Interaction):
        if not self.game_id_input.value.strip().isdigit():
            await interaction.response.send_message("Game ID must be a number.", ephemeral=True)
            return
        game_id = int(self.game_id_input.value.strip())
        if not self.new_faan_input.value.strip().isdigit():
            await interaction.response.send_message("Faan must be a number.", ephemeral=True)
            return
        new_faan = int(self.new_faan_input.value.strip())

        ids = await db.get_game_player_ids(game_id)
        if not ids:
            await interaction.response.send_message(f"No game found with id #{game_id}.", ephemeral=True)
            return
        if ids["win_type"] not in ("discard", "self_draw"):
            await interaction.response.send_message(
                "Only discard and self-draw wins have a faan count to edit.", ephemeral=True
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
            r = scoring.score_discard_win(new_faan)
            new_deltas[ids["winner_id"]] += r["winner"]
            new_deltas[ids["discarder_id"]] += r["discarder"]
        else:
            r = scoring.score_self_draw_win(new_faan)
            new_deltas[ids["winner_id"]] += r["winner"]
            for pid in ids["player_ids"]:
                if pid != ids["winner_id"]:
                    new_deltas[pid] += r["each_opponent"]

        await db.update_game_faan(game_id, new_faan, new_deltas)
        await game_actions.update_live_leaderboard(interaction.client)
        game = await db.get_game(game_id)
        lines = [f"{name}: {points:+d}" for name, points in game["scores"]]
        await interaction.response.send_message(
            f"✏️ Game #{game_id} updated to {new_faan} faan.\n" + "\n".join(lines), ephemeral=True
        )


class NewSeasonModal(discord.ui.Modal, title="Start a New Season"):
    name_input = discord.ui.TextInput(label="Season name (e.g. Fall 2026)", max_length=50)

    async def on_submit(self, interaction: discord.Interaction):
        name = self.name_input.value.strip()
        if not name:
            await interaction.response.send_message("Season name can't be empty.", ephemeral=True)
            return
        await db.create_new_season(name, str(interaction.user.id))

        channel_id = await db.get_setting("leaderboard_channel_id")
        posted_note = ""
        if channel_id:
            try:
                channel = interaction.client.get_channel(int(channel_id)) or await interaction.client.fetch_channel(int(channel_id))
                embed = await game_actions.build_leaderboard_embed()
                message = await channel.send(embed=embed)
                await db.set_setting("leaderboard_message_id", str(message.id))
                posted_note = f" A fresh leaderboard was posted in <#{channel_id}>."
            except Exception:
                posted_note = " (Couldn't auto-post a new leaderboard message -- check the leaderboard channel is still valid.)"

        await interaction.response.send_message(
            f"🎉 New season started: **{name}**.{posted_note}", ephemeral=True
        )


class BlacklistSelectView(discord.ui.View):
    def __init__(self, blacklist: bool):
        super().__init__(timeout=120)
        self.blacklist = blacklist
        self.add_item(self._make_select())

    def _make_select(self):
        select = discord.ui.UserSelect(
            placeholder=f"Select a player to {'blacklist' if self.blacklist else 'unblacklist'}",
            min_values=1,
            max_values=1,
        )

        async def callback(interaction: discord.Interaction):
            member = select.values[0]
            await db.get_or_create_player(str(member.id), member.display_name)
            await db.set_blacklisted(str(member.id), self.blacklist)
            verb = "blacklisted" if self.blacklist else "unblacklisted"
            await interaction.response.edit_message(content=f"{'🚫' if self.blacklist else '✅'} {member.display_name} has been {verb}.", view=None)

        select.callback = callback
        return select


class ModToolsView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Delete Game", emoji="🗑️", style=discord.ButtonStyle.danger, custom_id="mahjong_mod_delete", row=0)
    async def delete_game(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not _has_mod_permission(interaction):
            await interaction.response.send_message("You don't have permission to use this.", ephemeral=True)
            return
        await interaction.response.send_modal(DeleteGameModal())

    @discord.ui.button(label="Edit Game", emoji="✏️", style=discord.ButtonStyle.secondary, custom_id="mahjong_mod_edit", row=0)
    async def edit_game(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not _has_mod_permission(interaction):
            await interaction.response.send_message("You don't have permission to use this.", ephemeral=True)
            return
        await interaction.response.send_modal(EditGameModal())

    @discord.ui.button(label="Blacklist", emoji="🚫", style=discord.ButtonStyle.secondary, custom_id="mahjong_mod_blacklist", row=0)
    async def blacklist_player(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not _has_mod_permission(interaction):
            await interaction.response.send_message("You don't have permission to use this.", ephemeral=True)
            return
        await interaction.response.send_message(view=BlacklistSelectView(blacklist=True), ephemeral=True)

    @discord.ui.button(label="Unblacklist", emoji="✅", style=discord.ButtonStyle.secondary, custom_id="mahjong_mod_unblacklist", row=1)
    async def unblacklist_player(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not _has_mod_permission(interaction):
            await interaction.response.send_message("You don't have permission to use this.", ephemeral=True)
            return
        await interaction.response.send_message(view=BlacklistSelectView(blacklist=False), ephemeral=True)

    @discord.ui.button(label="View Blacklist", emoji="📋", style=discord.ButtonStyle.secondary, custom_id="mahjong_mod_viewbl", row=1)
    async def view_blacklist(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not _has_mod_permission(interaction):
            await interaction.response.send_message("You don't have permission to use this.", ephemeral=True)
            return
        rows = await db.get_blacklisted_players()
        if not rows:
            await interaction.response.send_message("No one is currently blacklisted.", ephemeral=True)
            return
        lines = [f"- {name}" for name, _ in rows]
        await interaction.response.send_message("**Blacklisted players:**\n" + "\n".join(lines), ephemeral=True)

    @discord.ui.button(label="New Season", emoji="🎉", style=discord.ButtonStyle.primary, custom_id="mahjong_mod_newseason", row=1)
    async def new_season(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not _has_mod_permission(interaction):
            await interaction.response.send_message("You don't have permission to use this.", ephemeral=True)
            return
        await interaction.response.send_modal(NewSeasonModal())

    @discord.ui.button(label="Export CSV", emoji="📊", style=discord.ButtonStyle.success, custom_id="mahjong_mod_export", row=2)
    async def export_csv(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not _has_mod_permission(interaction):
            await interaction.response.send_message("You don't have permission to use this.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        rows = await db.export_rows()
        if not rows:
            await interaction.followup.send("No games logged yet.", ephemeral=True)
            return

        buffer = io.StringIO()
        writer = csv.writer(buffer)
        writer.writerow(
            ["game_id", "timestamp_utc", "win_type", "faan", "player_name", "discord_id", "points", "is_winner", "is_discarder", "notes"]
        )
        for row in rows:
            game_id, ts, win_type, faan, player_name, discord_id, points, is_winner, is_discarder, notes = row
            dt = datetime.datetime.fromtimestamp(ts, tz=datetime.timezone.utc).isoformat()
            writer.writerow([game_id, dt, win_type, faan, player_name, discord_id, points, is_winner, is_discarder, notes])

        buffer.seek(0)
        file_bytes = io.BytesIO(buffer.getvalue().encode("utf-8"))
        filename = f"mahjong_export_{datetime.date.today().isoformat()}.csv"
        await interaction.followup.send(
            content="Here's your export — one row per player per game.",
            file=discord.File(file_bytes, filename=filename),
            ephemeral=True,
        )
