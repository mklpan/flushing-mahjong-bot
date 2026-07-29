"""
Button + modal UI for logging a game without typing a slash command.

Flow:
  1. Persistent "Log Game" button posted once via /setup
  2. Clicking it opens a modal asking for win type / faan / notes
  3. Submitting the modal shows an ephemeral message with dropdowns to
     pick the 4 seated players, the winner, and (if relevant) the discarder
  4. A "Submit Hand" button finalizes and records the game
"""

import discord

import game_actions
import scoring

WIN_TYPE_ALIASES = {
    "discard": "discard",
    "discard win": "discard",
    "self_draw": "self_draw",
    "self-draw": "self_draw",
    "self draw": "self_draw",
    "self draw win": "self_draw",
    "false_win": "false_win",
    "false win": "false_win",
    "draw": "draw",
    "void": "draw",
}


class LogGameButtonView(discord.ui.View):
    """Persistent view -- re-registered on bot startup so the button
    keeps working even after a restart/redeploy."""

    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(
        label="Log Game",
        emoji="🀄",
        style=discord.ButtonStyle.primary,
        custom_id="mahjong_log_game_button",
    )
    async def log_game_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(LogGameModal())


class LogGameModal(discord.ui.Modal, title="Log a Hand"):
    win_type_input = discord.ui.TextInput(
        label="Win type",
        placeholder="discard / self_draw / false_win / draw",
        required=True,
        max_length=20,
    )
    faan_input = discord.ui.TextInput(
        label="Faan (leave blank for false win / draw)",
        placeholder="e.g. 5",
        required=False,
        max_length=3,
    )
    notes_input = discord.ui.TextInput(
        label="Notes (optional)",
        required=False,
        max_length=200,
        style=discord.TextStyle.paragraph,
    )

    async def on_submit(self, interaction: discord.Interaction):
        raw = self.win_type_input.value.strip().lower()
        win_type = WIN_TYPE_ALIASES.get(raw)
        if win_type is None:
            await interaction.response.send_message(
                "Win type must be one of: `discard`, `self_draw`, `false_win`, `draw`. "
                "Click **Log Game** again to retry.",
                ephemeral=True,
            )
            return

        faan = None
        if win_type in ("discard", "self_draw"):
            raw_faan = self.faan_input.value.strip()
            if not raw_faan.isdigit():
                await interaction.response.send_message(
                    "Faan must be a whole number for discard/self-draw wins. "
                    "Click **Log Game** again to retry.",
                    ephemeral=True,
                )
                return
            faan = int(raw_faan)
            try:
                scoring.validate_win_faan(faan)
            except scoring.ScoringError as e:
                await interaction.response.send_message(str(e), ephemeral=True)
                return

        view = PlayerSelectView(
            win_type=win_type,
            faan=faan,
            notes=self.notes_input.value.strip() or None,
            requester_id=interaction.user.id,
        )
        await interaction.response.send_message(
            content=view.status_text(),
            view=view,
            ephemeral=True,
        )


class SeatedSelect(discord.ui.UserSelect):
    def __init__(self):
        super().__init__(
            placeholder="Select all 4 seated players",
            min_values=4,
            max_values=4,
            row=0,
        )

    async def callback(self, interaction: discord.Interaction):
        self.view.seated = list(self.values)
        await interaction.response.edit_message(content=self.view.status_text(), view=self.view)


class WinnerSelect(discord.ui.UserSelect):
    def __init__(self, label: str):
        super().__init__(placeholder=label, min_values=1, max_values=1, row=1)

    async def callback(self, interaction: discord.Interaction):
        self.view.winner = self.values[0]
        await interaction.response.edit_message(content=self.view.status_text(), view=self.view)


class DiscarderSelect(discord.ui.UserSelect):
    def __init__(self):
        super().__init__(
            placeholder="Select who discarded the winning tile", min_values=1, max_values=1, row=2
        )

    async def callback(self, interaction: discord.Interaction):
        self.view.discarder = self.values[0]
        await interaction.response.edit_message(content=self.view.status_text(), view=self.view)


class SubmitButton(discord.ui.Button):
    def __init__(self):
        super().__init__(label="Submit Hand", style=discord.ButtonStyle.success, row=3)

    async def callback(self, interaction: discord.Interaction):
        v = self.view
        if v.seated is None or len(v.seated) != 4:
            await interaction.response.edit_message(content="Please select all 4 seated players first.", view=v)
            return
        if v.win_type in ("discard", "self_draw") and v.winner is None:
            await interaction.response.edit_message(content="Please select the winner.", view=v)
            return
        if v.win_type == "discard" and v.discarder is None:
            await interaction.response.edit_message(content="Please select the discarder.", view=v)
            return
        if v.win_type == "false_win" and v.winner is None:
            await interaction.response.edit_message(
                content="Please select who made the false win call (use the winner dropdown).", view=v
            )
            return

        result = await game_actions.perform_log_game(
            seated=v.seated,
            win_type=v.win_type,
            winner=v.winner if v.win_type in ("discard", "self_draw") else None,
            faan=v.faan,
            discarder=v.discarder if v.win_type == "discard" else None,
            false_win_caller=v.winner if v.win_type == "false_win" else None,
            notes=v.notes,
            logged_by_id=str(interaction.user.id),
        )

        if not result["ok"]:
            await interaction.response.edit_message(content=f"⚠️ {result['error']}", view=None)
            return

        for item in v.children:
            item.disabled = True
        await interaction.response.edit_message(content="✅ Game logged!", embed=result["embed"], view=None)

        await game_actions.update_live_leaderboard(interaction.client)


class PlayerSelectView(discord.ui.View):
    def __init__(self, win_type: str, faan, notes, requester_id: int):
        super().__init__(timeout=300)
        self.win_type = win_type
        self.faan = faan
        self.notes = notes
        self.requester_id = requester_id

        self.seated = None
        self.winner = None
        self.discarder = None

        self.add_item(SeatedSelect())
        if win_type == "false_win":
            self.add_item(WinnerSelect("Select who made the false win call"))
        elif win_type in ("discard", "self_draw"):
            self.add_item(WinnerSelect("Select the winner"))
        if win_type == "discard":
            self.add_item(DiscarderSelect())
        self.add_item(SubmitButton())

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.requester_id:
            await interaction.response.send_message(
                "Only the person who started this /log-game flow can fill it out.", ephemeral=True
            )
            return False
        return True

    def status_text(self) -> str:
        lines = [f"**Logging a {self.win_type.replace('_', ' ')}**"]
        if self.faan is not None:
            lines.append(f"Faan: {self.faan}")
        lines.append(f"Seated: {', '.join(m.display_name for m in self.seated) if self.seated else '_not selected_'}")
        if self.win_type in ("discard", "self_draw", "false_win"):
            label = "False-win caller" if self.win_type == "false_win" else "Winner"
            lines.append(f"{label}: {self.winner.display_name if self.winner else '_not selected_'}")
        if self.win_type == "discard":
            lines.append(f"Discarder: {self.discarder.display_name if self.discarder else '_not selected_'}")
        lines.append("\nFill in all fields, then press **Submit Hand**.")
        return "\n".join(lines)
