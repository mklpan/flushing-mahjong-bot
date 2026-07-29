# Mahjong Club Bot

A Discord bot for logging Hong Kong / Cantonese mahjong games, tracking a
running leaderboard, and showing player stats. Scoring follows the
"half-spicy" faan table with these house rules:

- 3 faan minimum to win, 13 faan max (capped)
- False win: caller pays out at the 4-faan discard rate to each opponent
- 7 pairs allowed as a hand type (enter its actual faan value when logging)

## Commands

**Everyone:**
- Click the **Log Game** button (posted via `/setup-loggame`) — walks you through date/notes → faan/win type/players → winner/discarder, matching your club's exact workflow
- `/log-game` — same flow, but reachable without the button being visible
- `/leaderboard` — current season's standings
- `/stats [player]` — full stat card (current season **and** lifetime), including faan distributions, feeding stats, net discard given, and draw count
- `/recent-games` — last 10 games logged
- `/season-info` — see the current active season's name
- `/ping` — check the bot is alive

**Mods only** (require "Manage Server" by default — restrict further via **Server Settings → Integrations**):
- `/setup-loggame` — post the persistent Log Game button (do this once)
- `/setup-leaderboard` — post the live-updating leaderboard (do this once)
- `/setup-gamelog` — set this channel as where every logged game's card gets posted (do this once)
- `/setup-modtools` — post the mod tools button panel (do this once): Delete Game, Edit Game, Blacklist, Unblacklist, View Blacklist, New Season, Season Dates, Export CSV

### One-time setup checklist

Run these once, each in the channel you want them to live in:
1. `/setup-loggame` in your game-logging channel
2. `/setup-gamelog` in your game-history channel (can be the same channel or different)
3. `/setup-leaderboard` in your leaderboard channel
4. `/setup-modtools` in a mod-only channel

### Delete / Edit Game

Both now work by picking from a dropdown list of the 25 most recent games (labeled like `H42 · 2026-07-29 · Alice (Discard, 5f)`) rather than typing an ID.

- **Delete** shows a confirmation with the game's details before actually removing it — nothing is deleted on the first click.
- **Edit** re-opens the *exact same* multi-step flow used for logging a new hand (date/notes → faan/win type/players → winner/discarder), but every field starts pre-filled with that game's current values, so you can see exactly what you're changing. Submitting overwrites the original game in place — it does not create a duplicate.

### Seasons

Each season has a **number** and a **name** (e.g. "Season 2 (Fall 2026)"), shown in the leaderboard title and on player stat cards. The **New Season** button lets a mod set both explicitly (or leave the number blank to auto-increment), plus optional start/end dates. Past games stay tied to their original season permanently, so `/stats` always shows accurate season *and* lifetime numbers.

**Season date locking:** if a season has a start/end date set, any new game dated outside that window is rejected at submission time with a clear error. Use the **Season Dates** mod tool to add or update a date lock on the *currently active* season at any time (independent of starting a new season).

### Duplicate-submission protection

The Submit button disables itself the instant it's clicked (before any network calls), so mashing it can't create duplicate entries — the second click is a no-op.

### A note on "Net discard given"

This stat's exact original formula lives in a Google Sheets formula from the prior bot, not in that bot's code, so it wasn't possible to confirm byte-for-byte. It's implemented here as: **total points paid out specifically while in the discarder role** (excludes self-draw losses and false-win penalties). If you check the original spreadsheet formula and it differs, this is a one-line change in `database.py`.

### Setting up the button and live leaderboard

After deploying (see below), run through the **One-time setup checklist** above in your server.

**Note on bot permissions:** `/export-csv` sends a file attachment, which requires the **Attach Files** permission. If you invited the bot before this feature was added, re-generate your invite URL (OAuth2 → URL Generator) with `Attach Files` checked, and re-invite it (existing invites don't retroactively grant new permissions).

## Part 1: Create the Discord bot application

1. Go to https://discord.com/developers/applications and click **New Application**.
2. Name it (e.g. "Mahjong Bot"), then go to the **Bot** tab on the left.
3. Click **Reset Token** (or **Copy**) to get your bot token — save this somewhere
   safe, you'll need it below. Never share it publicly or commit it to GitHub.
4. Still on the Bot tab, make sure **Public Bot** is off if you don't want
   others adding it to their servers.
5. Go to the **OAuth2 → URL Generator** tab:
   - Under **Scopes**, check `bot` and `applications.commands`
   - Under **Bot Permissions**, check `Send Messages`, `Embed Links`, and `Attach Files`
   - Copy the generated URL at the bottom, open it in your browser, and
     select your club's server to invite the bot.

## Part 2: Run it locally (test before deploying)

1. Make sure you have Python 3.10+ installed.
2. In this folder, install dependencies:
   ```
   pip install -r requirements.txt
   ```
3. Copy `.env.example` to `.env` and paste in your bot token:
   ```
   cp .env.example .env
   ```
   Then edit `.env` so it looks like:
   ```
   DISCORD_BOT_TOKEN=your-actual-token-here
   ```
4. Run the bot:
   ```
   python bot.py
   ```
5. You should see `Logged in as Mahjong Bot#1234` in your terminal. In your
   Discord server, type `/ping` — the bot should reply "Pong! 🀄".

   Note: slash commands can take up to an hour to appear globally the very
   first time, but usually show up within a minute or two.

6. Try logging a test game with `/log-game` and check `/leaderboard` and
   `/stats` update correctly. Press Ctrl+C in the terminal to stop the bot.

## Part 3: Deploy to Railway (so it runs 24/7)

1. Push this folder to a GitHub repo (Railway deploys from GitHub).
   ```
   git init
   git add .
   git commit -m "Initial mahjong bot"
   ```
   Create a new repo on GitHub, then follow its instructions to push.

2. Go to https://railway.app and sign in with GitHub.
3. Click **New Project → Deploy from GitHub repo**, and select your repo.
4. Railway will detect the Python project automatically. Go to your new
   service's **Variables** tab and add:
   ```
   DISCORD_BOT_TOKEN=your-actual-token-here
   ```
5. **Important — persistent storage:** by default, Railway's filesystem
   resets on every redeploy, which would wipe `mahjong.db`. To keep your
   game history:
   - Go to your service → **Settings → Volumes**
   - Add a volume, mount it at `/app` (or a subfolder like `/app/data`)
   - If you mount it somewhere other than `/app`, update `DB_PATH` at the
     top of `database.py` to point inside that mounted folder
6. Railway should auto-deploy using the `Procfile` (`worker: python bot.py`).
   Check the **Deployments** tab for logs to confirm it connected
   successfully (same "Logged in as..." message as local testing).

Once deployed, you can stop running it locally — Railway keeps it online.

## Extending this later

Some natural next additions once this is running:
- A scheduled weekly leaderboard post
- `/undo-game` for correcting a misentry
- Season resets (e.g. archive points at the end of each month)
- Chombo/penalty tracking beyond false wins
- A `/head-to-head` command comparing two players
