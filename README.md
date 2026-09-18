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
- `/leaderboard [season_number]` — current season's standings, or a past season's if you give a number
- `/stats [player]` — full stat card (current season **and** lifetime)
- `/play-style [player]` — a radar chart (Attack/Defense/Aggression/Consistency) rendered as an actual image, requires 20+ hands in that scope to show
- `/head-to-head` — compare two players' record against each other (current season **and** lifetime)
- `/game-history [player]` — a player's game-by-game history (current season only, most recent first)
- `/recent-games` — last 10 games logged
- `/season-list` — every season this club has had, with its date range and current-season marker
- `/hall-of-fame` — top 10 finishers from every **completed** season (the current in-progress season doesn't appear until it ends)
- `/season-dashboard` — season-wide stats: total hands, players, days played, average faan, hand-type breakdown, and faan distributions (overall, and split by discard vs. self-draw wins)
- `/leaderboard-lifetime` — all-time standings across every season combined
- `/ping` — check the bot is alive

**Mods only** (require "Manage Server" by default — restrict further via **Server Settings → Integrations**):
- `/setup-loggame` — post the persistent Log Game button (do this once)
- `/setup-leaderboard` — post the live-updating season leaderboard (do this once)
- `/setup-leaderboard-lifetime` — post a live-updating all-time leaderboard (do this once)
- `/setup-hall-of-fame` — post a live-updating Hall of Fame (do this once)
- `/setup-season-dashboard` — post a live-updating season-wide stats dashboard (do this once)
- `/setup-gamelog` — set this channel as where every logged game's card gets posted (do this once)
- `/setup-modtools` — post the mod tools button panel (do this once): Delete Game, Edit Game, Blacklist, Unblacklist, View Blacklist, New Season, Edit Season, Reset Season, Export CSV

### One-time setup checklist

Run these once, each in the channel you want them to live in:
1. `/setup-loggame` in your game-logging channel
2. `/setup-gamelog` in your game-history channel (can be the same channel or different)
3. `/setup-leaderboard` in your leaderboard channel
4. `/setup-leaderboard-lifetime` in a channel for the all-time board (can be the same channel)
5. `/setup-hall-of-fame` in a channel for completed-season results
6. `/setup-season-dashboard` in a channel for season-wide stats
7. `/setup-modtools` in a mod-only channel

### Reset Season

**Reset Season** permanently deletes every game logged in the *current* season only — other seasons, blacklist entries, and all channel setup are completely untouched. It requires typing the exact word `RESET` into a confirmation modal (not just a button click) given how destructive it is. Any player left with zero games anywhere after the reset is also cleaned up, so old test-only players don't linger as ghost 0-point entries on the lifetime leaderboard.

This is meant for exactly one situation: testing the bot for real (logging real test hands, trying mod tools, etc.) in a season you plan to throw away before actually opening things up to real players — not for correcting a mistake mid-season, which `/delete-game` already handles more precisely.

### Delete / Edit Game

Both now work by picking from a dropdown list of the 25 most recent games (labeled like `H42 · 2026-07-29 · Alice (Discard, 5f)`) rather than typing an ID.

- **Delete** shows a confirmation with the game's details before actually removing it — nothing is deleted on the first click.
- **Edit** re-opens the *exact same* multi-step flow used for logging a new hand (date/notes → faan/win type/players → winner/discarder), but every field starts pre-filled with that game's current values, so you can see exactly what you're changing. Submitting overwrites the original game in place — it does not create a duplicate.

### Seasons

Each season has a **number** and a **name** (e.g. "Season 2 (Fall 2026)"), shown in the leaderboard title and on player stat cards. The **New Season** button lets a mod set both explicitly (or leave the number blank to auto-increment), plus optional start/end dates. Past games stay tied to their original season permanently, so `/stats` always shows accurate season *and* lifetime numbers.

**When a new season starts, the old leaderboard message is automatically deleted from the channel** to keep things tidy — nothing is lost, it's just no longer cluttering the channel. Past seasons remain fully viewable via `/leaderboard <season_number>`, `/hall-of-fame` (top 10 from every completed season), `/leaderboard-lifetime`, `/season-list`, and the CSV export (which now includes season number/name columns for filtering in Power BI/Tableau).

### Season date locking

If a season has a start/end date set, any new game dated outside that window is rejected at submission time with a clear error. Use the **Edit Season** mod tool to add or update a date lock on the *currently active* season at any time (independent of starting a new season).

### Remembering seated players

When you log a new hand, the bot remembers the 4 players you were seated with and pre-fills them automatically the next time you log a hand that same day — handy for logging several hands from the same table in a row. This resets at midnight **US/Eastern**, so it never carries over to a new day. It only applies to *new* hands, not edits, and only for the person who actually logged the game (each mod/player has their own separate memory).

### Duplicate-submission protection

The Submit button disables itself the instant it's clicked (before any network calls), so mashing it can't create duplicate entries — the second click is a no-op.

### A note on "Net discard given"

This stat's exact original formula lives in a Google Sheets formula from the prior bot, not in that bot's code, so it wasn't possible to confirm byte-for-byte. It's implemented here as: **total points paid out specifically while in the discarder role** (excludes self-draw losses and false-win penalties). If you check the original spreadsheet formula and it differs, this is a one-line change in `database.py`.

### Play Style chart

`/play-style` renders an actual radar chart image using `matplotlib` (added as a dependency — Railway will install it automatically on your next deploy, no manual setup needed). The four axes are custom-defined for this bot rather than pulled from an existing standard:

**Attack — how hard you hit when you win**
```
Attack = average points earned per winning hand (capped at 100)
```
Add up all the points you've earned across every hand you've won, divide by your number of wins.
*Example:* wins worth 24, 16, and 8 points → Attack = (24+16+8)/3 = 16.

**Defense — how well you avoid feeding others**
```
Defense = 100 − (times you discarded into a loss ÷ total hands played) × 100
```
How often you're specifically the discarder in a hand someone else wins, relative to hands played.
*Example:* 20 hands played, discarded into a loss 4 times → Defense = 100 − (4/20 × 100) = 80.

**Aggression — how big the hands you go for are**
```
Aggression = (average faan on your wins − 3) ÷ (13 − 3) × 100
```
Maps your typical winning faan onto a 0-100 scale (3 faan = the minimum winning hand = 0, 13 faan = the max = 100).
*Example:* wins average 6 faan → Aggression = (6−3)/(13−3) × 100 = 30.

**Consistency — how often you actually win**
```
Consistency = win rate = (wins ÷ total hands) × 100
```
Same win percentage already shown on the leaderboard.

Requires at least 20 hands in a scope (season or lifetime) before it'll render — below that threshold the numbers would be too noisy to mean much, so it shows a "not enough data yet" message instead. These aren't official mahjong statistics — they were custom-designed for this bot based on data it already tracks, and all four formulas are easy to adjust in `game_actions.py`'s `_compute_playstyle_scores` if any of them ever feel off.

### Automated weekly backups (optional, recommended)

If `BACKUP_WEBHOOK_URL` is set (see `.env.example`), the bot automatically posts a fresh CSV export every **Sunday at 9am US/Eastern** to a webhook URL you control — designed specifically to deliver to a channel in a *different* server than the club server (e.g. your own personal server), giving you a backup that's genuinely independent of both Railway and the club server itself.

**Setup:**
1. In whichever server/channel you want backups delivered to: **Channel Settings → Integrations → Webhooks → New Webhook**
2. Copy the webhook URL
3. Add it as `BACKUP_WEBHOOK_URL` in Railway's Variables tab (and your local `.env` if you want to test it)

The bot does **not** need to be a member of that server for this to work — webhooks deliver independently of bot membership. If the variable is left unset, this feature is simply disabled with no other effect.

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
