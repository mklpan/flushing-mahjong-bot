"""
Button + modal UI for the mahjong bot.

Log-game flow:
  1. Persistent "Log Game" button (posted via /setup-loggame) -> DateNotesModal
  2. DateNotesModal (date defaults to today, notes) -> GameDetailsView
  3. GameDetailsView: faan / win type / 4 seated players (faan dropdown
     dynamically disappears if win type is False Win or Draw) -> Next.
     Every select shows its current choice as pre-selected, even across
     the dynamic rebuild that happens when win type changes.
  4. RoleSelectView: winner+discarder (discard), winner (self-draw),
     false-win caller (false win), or nothing (draw) -> Submit Hand
  5. On submit: card posted to the game-log channel, live leaderboard
     refreshed, and the ephemeral flow message deletes itself.

Edit flow: mod tools -> Edit Game -> pick from a list of recent games ->
the exact same modal/prompt sequence as logging a new hand, but every
field starts pre-filled with that game's current values so mods can see
what they're changing. Submitting overwrites the original game in place.

Mod tools flow:
  A persistent button panel (posted via /setup-modtools) with buttons for
  delete/edit game (both via a game picker), blacklist/unblacklist, view
  blacklist, new season, season dates, and CSV export -- restricted to
  users with the Manage Server permission as a code-level backup to the
  channel itself being mod-only.
"""

import csv
import io
import datetime
from zoneinfo import ZoneInfo
import discord

import database as db
import scoring
import game_actions

EASTERN = ZoneInfo("America/New_York")


def _eastern_today_str():
    return datetime.datetime.now(EASTERN).date().isoformat()


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


async def _resolve_members(guild: discord.Guild, discord_ids):
    """Look up live discord.Member objects for a list of discord IDs
    (strings). Returns (members, missing_ids) -- missing_ids is non-empty
    if someone has left the server since the game was logged."""
    members = []
    missing = []
    for did in discord_ids:
        member = guild.get_member(int(did))
        if member is None:
            try:
                member = await guild.fetch_member(int(did))
            except discord.NotFound:
                member = None
        if member is None:
            missing.append(did)
        else:
            members.append(member)
    return members, missing


async def _finish_log_flow(interaction: discord.Interaction, result: dict, seated=None, is_edit=False):
    """Shared tail end for every win-type path: post the card to the
    game-log channel (or fall back to the current channel), refresh the
    live leaderboard, remember the seated players for next time (new logs
    only), and make the ephemeral flow message disappear."""
    if not result["ok"]:
        await interaction.edit_original_response(content=f"⚠️ {result['error']}", embed=None, view=None)
        return

    posted = await game_actions.post_logged_game(interaction.client, result["embed"])
    if not posted:
        await interaction.channel.send(embed=result["embed"])

    await game_actions.refresh_boards(interaction.client)

    if not is_edit and seated:
        await db.set_last_seated(str(interaction.user.id), [m.id for m in seated], _eastern_today_str())

    try:
        await interaction.delete_original_response()
    except Exception:
        await interaction.edit_original_response(content="✅ Done!", embed=None, view=None)


# ---------------------------------------------------------------------------
# Step 1: persistent button + date/notes modal
# ---------------------------------------------------------------------------

class DateNotesModal(discord.ui.Modal):
    """Used both for logging a brand-new hand and as the first step of
    editing an existing one. When editing, every field is pre-filled with
    the original game's data via the `edit_*` / `prefill_*` kwargs."""

    date_input = discord.ui.TextInput(label="Date (YYYY-MM-DD)", required=False, max_length=10)
    notes_input = discord.ui.TextInput(
        label="Notes (optional)", required=False, max_length=200, style=discord.TextStyle.paragraph
    )

    def __init__(
        self,
        edit_game_id: int = None,
        edit_display_ref: str = None,
        prefill_seated=None,
        prefill_faan=None,
        prefill_win_type=None,
        prefill_winner=None,
        prefill_discarder=None,
        prefill_false_winner=None,
    ):
        ref = edit_display_ref or (f"H{edit_game_id}" if edit_game_id else None)
        title = f"Edit Hand {ref}" if edit_game_id else "Log a Hand — Date & Notes"
        super().__init__(title=title)
        self.edit_game_id = edit_game_id
        self.edit_display_ref = ref
        self.prefill_seated = prefill_seated
        self.prefill_faan = prefill_faan
        self.prefill_win_type = prefill_win_type
        self.prefill_winner = prefill_winner
        self.prefill_discarder = prefill_discarder
        self.prefill_false_winner = prefill_false_winner

    async def on_submit(self, interaction: discord.Interaction):
        raw_date = self.date_input.value.strip()
        if raw_date:
            try:
                datetime.date.fromisoformat(raw_date)
            except ValueError:
                await interaction.response.send_message(
                    "Date must be in YYYY-MM-DD format (e.g. 2026-07-29). Please try again.",
                    ephemeral=True,
                )
                return
            game_date = raw_date
        else:
            game_date = datetime.date.today().isoformat()

        initial_seated = self.prefill_seated
        if self.edit_game_id is None:
            # Only for NEW hands (not edits) -- recall the last 4 seated
            # players this user logged with today (US/Eastern), so they
            # don't have to re-pick the same table every single hand.
            last = await db.get_last_seated(str(interaction.user.id))
            if last and last["updated_date"] == _eastern_today_str() and interaction.guild:
                resolved, _missing = await _resolve_members(interaction.guild, last["player_ids"])
                if resolved:
                    initial_seated = resolved

        view = GameDetailsView(
            game_date=game_date,
            notes=self.notes_input.value.strip() or None,
            requester_id=interaction.user.id,
            edit_game_id=self.edit_game_id,
            edit_display_ref=self.edit_display_ref,
            initial_seated=initial_seated,
            initial_faan=self.prefill_faan,
            initial_win_type=self.prefill_win_type,
            initial_winner=self.prefill_winner,
            initial_discarder=self.prefill_discarder,
            initial_false_winner=self.prefill_false_winner,
        )
        await interaction.response.send_message(content=view.status_text(), view=view, ephemeral=True)


def _make_date_notes_modal_for_new():
    modal = DateNotesModal()
    modal.date_input.default = datetime.date.today().isoformat()
    return modal


def _make_date_notes_modal_for_edit(edit_game_id, game_date, notes, seated, faan, win_type, winner, discarder, false_winner, edit_display_ref=None):
    modal = DateNotesModal(
        edit_game_id=edit_game_id,
        edit_display_ref=edit_display_ref,
        prefill_seated=seated,
        prefill_faan=faan,
        prefill_win_type=win_type,
        prefill_winner=winner,
        prefill_discarder=discarder,
        prefill_false_winner=false_winner,
    )
    modal.date_input.default = game_date
    modal.notes_input.default = notes
    return modal


class LogGameButtonView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(
        label="Log Game", emoji="🀄", style=discord.ButtonStyle.primary, custom_id="mahjong_log_game_button"
    )
    async def log_game_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(_make_date_notes_modal_for_new())


# ---------------------------------------------------------------------------
# Step 2: faan / win type / seated players
# ---------------------------------------------------------------------------

class FaanSelect(discord.ui.Select):
    def __init__(self, selected=None):
        options = []
        for opt in FAAN_OPTIONS:
            o = discord.SelectOption(label=opt.label, value=opt.value, default=(selected is not None and opt.value == str(selected)))
            options.append(o)
        super().__init__(placeholder="Faan (3-13)", options=options, min_values=1, max_values=1, row=0)

    async def callback(self, interaction: discord.Interaction):
        view = self.view
        view.faan = int(self.values[0])
        for opt in self.options:
            opt.default = opt.value == self.values[0]
        await interaction.response.edit_message(content=view.status_text(), view=view)


class WinTypeSelect(discord.ui.Select):
    def __init__(self, selected=None):
        options = []
        for opt in WIN_TYPE_OPTIONS:
            o = discord.SelectOption(label=opt.label, value=opt.value, default=(selected == opt.value))
            options.append(o)
        super().__init__(placeholder="Win type", options=options, min_values=1, max_values=1, row=1)

    async def callback(self, interaction: discord.Interaction):
        view = self.view  # grab this BEFORE rebuild_items() detaches self from it
        view.win_type = self.values[0]
        if not _requires_faan(view.win_type):
            view.faan = None
        view.rebuild_items()
        await interaction.response.edit_message(content=view.status_text(), view=view)


class SeatedSelect(discord.ui.UserSelect):
    def __init__(self, default_values=None):
        super().__init__(
            placeholder="Select all 4 seated players",
            min_values=4,
            max_values=4,
            row=2,
            default_values=default_values or [],
        )

    async def callback(self, interaction: discord.Interaction):
        view = self.view
        view.seated = list(self.values)
        self.default_values = list(self.values)
        await interaction.response.edit_message(content=view.status_text(), view=view)


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
            await interaction.response.edit_message(content="Logging...", view=None)
            if v.edit_game_id:
                result = await game_actions.perform_edit_game(
                    game_id=v.edit_game_id, seated=v.seated, win_type="draw",
                    notes=v.notes, game_date=v.game_date,
                    edited_by_name=interaction.user.display_name,
                )
            else:
                result = await game_actions.perform_log_game(
                    seated=v.seated, win_type="draw", notes=v.notes,
                    logged_by_id=str(interaction.user.id),
                    logged_by_name=interaction.user.display_name,
                    game_date=v.game_date,
                )
            await _finish_log_flow(interaction, result, seated=v.seated, is_edit=bool(v.edit_game_id))
            return

        role_view = RoleSelectView(parent=v)
        await interaction.response.edit_message(content=role_view.status_text(), view=role_view)


class GameDetailsView(discord.ui.View):
    def __init__(
        self,
        game_date: str,
        notes,
        requester_id: int,
        edit_game_id: int = None,
        edit_display_ref: str = None,
        initial_seated=None,
        initial_faan=None,
        initial_win_type=None,
        initial_winner=None,
        initial_discarder=None,
        initial_false_winner=None,
    ):
        super().__init__(timeout=300)
        self.game_date = game_date
        self.notes = notes
        self.requester_id = requester_id
        self.edit_game_id = edit_game_id
        self.edit_display_ref = edit_display_ref or (f"H{edit_game_id}" if edit_game_id else None)

        self.faan = initial_faan
        self.win_type = initial_win_type
        self.seated = initial_seated

        # Carried through to RoleSelectView so the winner/discarder/etc.
        # also start pre-filled when editing.
        self.initial_winner = initial_winner
        self.initial_discarder = initial_discarder
        self.initial_false_winner = initial_false_winner

        self.rebuild_items()

    def rebuild_items(self):
        self.clear_items()
        if _requires_faan(self.win_type) or self.win_type is None:
            self.add_item(FaanSelect(selected=self.faan))
        self.add_item(WinTypeSelect(selected=self.win_type))
        self.add_item(SeatedSelect(default_values=self.seated))
        self.add_item(NextButton())

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.requester_id:
            await interaction.response.send_message(
                "Only the person who started this flow can fill it out.", ephemeral=True
            )
            return False
        return True

    def status_text(self) -> str:
        prefix = f"**Editing {self.edit_display_ref}**" if self.edit_game_id else f"**Logging a hand — {self.game_date}**"
        lines = [prefix]
        if self.edit_game_id:
            lines.append(f"Date: {self.game_date}")
        if self.notes:
            lines.append(f"Notes: {self.notes}")
        if _requires_faan(self.win_type):
            lines.append(f"Faan: {self.faan if self.faan is not None else '_not selected_'}")
        else:
            lines.append("Faan: _n/a for this win type_")
        lines.append(f"Win type: {self.win_type or '_not selected_'}")
        lines.append(f"Seated: {', '.join(m.display_name for m in self.seated) if self.seated else '_not selected_'}")
        lines.append("\nFill in the fields above, then press **Next**.")
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# Step 3: role selection (winner / discarder / false-win caller)
# ---------------------------------------------------------------------------

class MemberChoiceSelect(discord.ui.Select):
    def __init__(self, members, placeholder, row, attr_name, selected_member=None):
        options = [
            discord.SelectOption(
                label=m.display_name, value=str(m.id), default=(selected_member is not None and m.id == selected_member.id)
            )
            for m in members
        ]
        super().__init__(placeholder=placeholder, options=options, min_values=1, max_values=1, row=row)
        self.attr_name = attr_name

    async def callback(self, interaction: discord.Interaction):
        view = self.view
        member_id = int(self.values[0])
        member = next(m for m in view.parent.seated if m.id == member_id)
        setattr(view, self.attr_name, member)
        for opt in self.options:
            opt.default = opt.value == self.values[0]
        await interaction.response.edit_message(content=view.status_text(), view=view)


class RoleSubmitButton(discord.ui.Button):
    def __init__(self):
        super().__init__(label="Submit Hand", style=discord.ButtonStyle.success, row=2)

    async def callback(self, interaction: discord.Interaction):
        v: RoleSelectView = self.view

        if v.submitted:
            return
        v.submitted = True
        for item in v.children:
            item.disabled = True

        p = v.parent
        win_type = p.win_type

        def unlock_and_retry(msg):
            v.submitted = False
            for item in v.children:
                item.disabled = False
            return msg

        if win_type == "discard" and (v.winner is None or v.discarder is None):
            await interaction.response.edit_message(content=unlock_and_retry("Please select both winner and discarder."), view=v)
            return
        if win_type == "self_draw" and v.winner is None:
            await interaction.response.edit_message(content=unlock_and_retry("Please select the winner."), view=v)
            return
        if win_type == "false_win" and v.false_winner is None:
            await interaction.response.edit_message(content=unlock_and_retry("Please select who made the false win call."), view=v)
            return

        await interaction.response.edit_message(content="Logging...", view=v)

        common_kwargs = dict(
            seated=p.seated,
            win_type=win_type,
            winner=v.winner if win_type in ("discard", "self_draw") else None,
            faan=p.faan,
            discarder=v.discarder if win_type == "discard" else None,
            false_win_caller=v.false_winner if win_type == "false_win" else None,
            notes=p.notes,
            game_date=p.game_date,
        )

        if p.edit_game_id:
            result = await game_actions.perform_edit_game(
                game_id=p.edit_game_id, edited_by_name=interaction.user.display_name, **common_kwargs
            )
        else:
            result = await game_actions.perform_log_game(
                logged_by_id=str(interaction.user.id),
                logged_by_name=interaction.user.display_name,
                **common_kwargs,
            )

        await _finish_log_flow(interaction, result, seated=p.seated, is_edit=bool(p.edit_game_id))


class RoleSelectView(discord.ui.View):
    def __init__(self, parent: GameDetailsView):
        super().__init__(timeout=300)
        self.parent = parent
        self.winner = parent.initial_winner
        self.discarder = parent.initial_discarder
        self.false_winner = parent.initial_false_winner
        self.submitted = False

        if parent.win_type == "discard":
            self.add_item(MemberChoiceSelect(parent.seated, "Select the winner", 0, "winner", selected_member=self.winner))
            self.add_item(MemberChoiceSelect(parent.seated, "Select the discarder", 1, "discarder", selected_member=self.discarder))
        elif parent.win_type == "self_draw":
            self.add_item(MemberChoiceSelect(parent.seated, "Select the self-draw winner", 0, "winner", selected_member=self.winner))
        elif parent.win_type == "false_win":
            self.add_item(MemberChoiceSelect(parent.seated, "Select who made the false win call", 0, "false_winner", selected_member=self.false_winner))
        self.add_item(RoleSubmitButton())

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.parent.requester_id:
            await interaction.response.send_message(
                "Only the person who started this flow can fill it out.", ephemeral=True
            )
            return False
        return True

    def status_text(self) -> str:
        prefix = f"**Editing {self.parent.edit_display_ref}**" if self.parent.edit_game_id else f"**{self.parent.win_type.replace('_', ' ').title()} — {self.parent.game_date}**"
        lines = [prefix]
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
# Game picker (used by Delete Game and Edit Game)
# ---------------------------------------------------------------------------

def _format_game_option_label(row):
    game_id, win_type, faan, game_date, winner_name, discarder_name, season_number, season_game_number = row
    type_label = {"discard": "Discard", "self_draw": "Self-draw", "false_win": "False win", "draw": "Draw"}.get(win_type, win_type)
    faan_part = f", {faan}f" if faan else ""
    who = winner_name or "—"
    hand_ref = f"S{season_number}-H{season_game_number}" if season_number is not None else f"H{game_id}"
    label = f"{hand_ref} · {game_date or '?'} · {who} ({type_label}{faan_part})"
    return label[:100]


class GamePickerSelect(discord.ui.Select):
    def __init__(self, rows, action):
        options = [discord.SelectOption(label=_format_game_option_label(r), value=str(r[0])) for r in rows]
        super().__init__(placeholder=f"Select a game to {action}", options=options, min_values=1, max_values=1)
        self.action = action

    async def callback(self, interaction: discord.Interaction):
        game_id = int(self.values[0])
        if self.action == "delete":
            game = await db.get_game(game_id)
            if not game:
                await interaction.response.edit_message(content=f"Game #{game_id} no longer exists.", view=None)
                return
            lines = [f"{name}: {points:+d}" for name, points in game["scores"]]
            hand_ref = f"S{game['season_number']}-H{game['season_game_number']}" if game["season_number"] is not None else f"H{game_id}"
            confirm_view = ConfirmDeleteView(game_id=game_id)
            await interaction.response.edit_message(
                content=(
                    f"**Delete {hand_ref}?** ({game['win_type']}"
                    + (f", {game['faan']} faan" if game["faan"] else "")
                    + f")\n" + "\n".join(lines) + "\n\nThis cannot be undone."
                ),
                view=confirm_view,
            )
        else:  # edit
            game = await db.get_game_for_edit(game_id)
            if not game:
                await interaction.response.edit_message(content=f"Game #{game_id} no longer exists.", view=None)
                return

            discord_ids = [p[1] for p in game["players"]]
            members, missing = await _resolve_members(interaction.guild, discord_ids)
            hand_ref = f"S{game['season_number']}-H{game['season_game_number']}" if game["season_number"] is not None else f"H{game_id}"
            if missing:
                await interaction.response.edit_message(
                    content=f"Can't edit {hand_ref}: {len(missing)} seated player(s) are no longer in this server.",
                    view=None,
                )
                return

            id_to_member = {str(m.id): m for m in members}
            player_id_to_discord_id = {p[0]: p[1] for p in game["players"]}

            def member_for(player_id):
                if player_id is None:
                    return None
                did = player_id_to_discord_id.get(player_id)
                return id_to_member.get(did)

            modal = _make_date_notes_modal_for_edit(
                edit_game_id=game_id,
                edit_display_ref=hand_ref,
                game_date=game["game_date"] or datetime.date.today().isoformat(),
                notes=game["notes"],
                seated=members,
                faan=game["faan"],
                win_type=game["win_type"],
                winner=member_for(game["winner_id"]),
                discarder=member_for(game["discarder_id"]),
                false_winner=member_for(game["winner_id"]) if game["win_type"] == "false_win" else None,
            )
            await interaction.response.send_modal(modal)


class GamePickerView(discord.ui.View):
    def __init__(self, rows, action):
        super().__init__(timeout=120)
        self.add_item(GamePickerSelect(rows, action))


class ConfirmDeleteView(discord.ui.View):
    def __init__(self, game_id: int):
        super().__init__(timeout=60)
        self.game_id = game_id
        self.confirmed = False

    @discord.ui.button(label="Confirm Delete", style=discord.ButtonStyle.danger)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        if self.confirmed:
            return
        self.confirmed = True
        game = await db.get_game(self.game_id)
        if not game:
            await interaction.response.edit_message(content="Already deleted.", view=None)
            return
        await db.delete_game(self.game_id)
        await game_actions.refresh_boards(interaction.client)
        lines = [f"{name}: {points:+d}" for name, points in game["scores"]]
        hand_ref = f"S{game['season_number']}-H{game['season_game_number']}" if game["season_number"] is not None else f"H{self.game_id}"
        await interaction.response.edit_message(
            content=f"🗑️ Deleted {hand_ref}. Reversed:\n" + "\n".join(lines), view=None
        )

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.edit_message(content="Cancelled.", view=None)


# ---------------------------------------------------------------------------
# Mod tools panel
# ---------------------------------------------------------------------------

class NewSeasonModal(discord.ui.Modal, title="Start a New Season"):
    name_input = discord.ui.TextInput(label="Season name (e.g. Fall 2026)", max_length=50)
    number_input = discord.ui.TextInput(label="Season # (blank = auto)", required=False, max_length=5)
    start_date_input = discord.ui.TextInput(label="Start date YYYY-MM-DD (optional)", required=False, max_length=10)
    end_date_input = discord.ui.TextInput(label="End date YYYY-MM-DD (optional)", required=False, max_length=10)

    async def on_submit(self, interaction: discord.Interaction):
        name = self.name_input.value.strip()
        if not name:
            await interaction.response.send_message("Season name can't be empty.", ephemeral=True)
            return

        number = None
        if self.number_input.value.strip():
            if not self.number_input.value.strip().isdigit():
                await interaction.response.send_message("Season # must be a number.", ephemeral=True)
                return
            number = int(self.number_input.value.strip())

        for label, val in (("start date", self.start_date_input.value), ("end date", self.end_date_input.value)):
            if val.strip():
                try:
                    datetime.date.fromisoformat(val.strip())
                except ValueError:
                    await interaction.response.send_message(f"{label.title()} must be YYYY-MM-DD.", ephemeral=True)
                    return

        start_date = self.start_date_input.value.strip() or None
        end_date = self.end_date_input.value.strip() or None

        # Grab the OLD leaderboard message location before we overwrite the
        # setting, so we can hide it once the new season's board is posted.
        old_channel_id = await db.get_setting("leaderboard_channel_id")
        old_message_id = await db.get_setting("leaderboard_message_id")

        await db.create_new_season(name, str(interaction.user.id), season_number=number, start_date=start_date, end_date=end_date)

        channel_id = old_channel_id
        posted_note = ""
        if channel_id:
            try:
                channel = interaction.client.get_channel(int(channel_id)) or await interaction.client.fetch_channel(int(channel_id))

                # Hide the previous season's leaderboard message -- its data
                # isn't gone (still in the database, viewable via /leaderboard
                # <season>, /hall-of-fame, and CSV export), just removed from
                # the channel to keep things from cluttering up over time.
                if old_message_id:
                    try:
                        old_message = await channel.fetch_message(int(old_message_id))
                        await old_message.delete()
                    except Exception:
                        pass

                embed = await game_actions.build_leaderboard_embed()
                message = await channel.send(embed=embed)
                await db.set_setting("leaderboard_message_id", str(message.id))
                posted_note = f" A fresh leaderboard was posted in <#{channel_id}>."
            except Exception:
                posted_note = " (Couldn't auto-post a new leaderboard message -- check the leaderboard channel is still valid.)"

        # The season that just ended is now eligible for the Hall of Fame.
        await game_actions.update_live_hall_of_fame(interaction.client)
        await game_actions.update_live_lifetime_leaderboard(interaction.client)

        await interaction.response.send_message(
            f"🎉 New season started: **{name}**.{posted_note} Use `/hall-of-fame` to see top finishers from every past season.",
            ephemeral=True,
        )


class EditSeasonModal(discord.ui.Modal, title="Edit Current Season"):
    name_input = discord.ui.TextInput(label="Season name (e.g. Fall 2026)", max_length=50)
    number_input = discord.ui.TextInput(label="Season #", max_length=5)
    start_date_input = discord.ui.TextInput(label="Start date YYYY-MM-DD (blank = no lock)", required=False, max_length=10)
    end_date_input = discord.ui.TextInput(label="End date YYYY-MM-DD (blank = no lock)", required=False, max_length=10)

    def __init__(self, season: dict):
        super().__init__()
        self.season = season
        self.name_input.default = season["name"]
        self.number_input.default = str(season["number"]) if season["number"] is not None else ""
        self.start_date_input.default = season["start_date"] or ""
        self.end_date_input.default = season["end_date"] or ""

    async def on_submit(self, interaction: discord.Interaction):
        name = self.name_input.value.strip()
        if not name:
            await interaction.response.send_message("Season name can't be empty.", ephemeral=True)
            return

        if not self.number_input.value.strip().isdigit():
            await interaction.response.send_message("Season # must be a number.", ephemeral=True)
            return
        number = int(self.number_input.value.strip())

        for label, val in (("start date", self.start_date_input.value), ("end date", self.end_date_input.value)):
            if val.strip():
                try:
                    datetime.date.fromisoformat(val.strip())
                except ValueError:
                    await interaction.response.send_message(f"{label.title()} must be YYYY-MM-DD.", ephemeral=True)
                    return

        start_date = self.start_date_input.value.strip() or None
        end_date = self.end_date_input.value.strip() or None

        await db.update_season_info(self.season["id"], name, number, start_date, end_date)
        await game_actions.refresh_boards(interaction.client)

        lock_desc = "no date lock" if not (start_date or end_date) else f"{start_date or 'open'} → {end_date or 'open'}"
        await interaction.response.send_message(
            f"✅ Season updated: **Season {number} ({name})** · {lock_desc}", ephemeral=True
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
        rows = await db.get_recent_games_for_picker(25)
        if not rows:
            await interaction.response.send_message("No games logged yet.", ephemeral=True)
            return
        await interaction.response.send_message(view=GamePickerView(rows, "delete"), ephemeral=True)

    @discord.ui.button(label="Edit Game", emoji="✏️", style=discord.ButtonStyle.secondary, custom_id="mahjong_mod_edit", row=0)
    async def edit_game(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not _has_mod_permission(interaction):
            await interaction.response.send_message("You don't have permission to use this.", ephemeral=True)
            return
        rows = await db.get_recent_games_for_picker(25)
        if not rows:
            await interaction.response.send_message("No games logged yet.", ephemeral=True)
            return
        await interaction.response.send_message(view=GamePickerView(rows, "edit"), ephemeral=True)

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

    @discord.ui.button(label="Edit Season", emoji="📅", style=discord.ButtonStyle.secondary, custom_id="mahjong_mod_seasondates", row=2)
    async def season_dates(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not _has_mod_permission(interaction):
            await interaction.response.send_message("You don't have permission to use this.", ephemeral=True)
            return
        season = await db.get_active_season()
        if not season:
            await interaction.response.send_message("No active season.", ephemeral=True)
            return
        await interaction.response.send_modal(EditSeasonModal(season))

    @discord.ui.button(label="Export CSV", emoji="📊", style=discord.ButtonStyle.success, custom_id="mahjong_mod_export", row=2)
    async def export_csv(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not _has_mod_permission(interaction):
            await interaction.response.send_message("You don't have permission to use this.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        filename, file_bytes = await game_actions.build_export_csv()
        if filename is None:
            await interaction.followup.send("No games logged yet.", ephemeral=True)
            return

        await interaction.followup.send(
            content="Here's your export — one row per player per game.",
            file=discord.File(file_bytes, filename=filename),
            ephemeral=True,
        )
