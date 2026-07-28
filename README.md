# Mahjong Club Bot

A Discord bot for logging Hong Kong / Cantonese mahjong games, tracking a
running leaderboard, and showing player stats. Scoring follows the
"half-spicy" faan table with these house rules:

- 3 faan minimum to win, 13 faan max (capped)
- False win: caller pays out at the 4-faan discard rate to each opponent
- 7 pairs allowed as a hand type (enter its actual faan value when logging)

## Commands

- `/log-game` — log a completed hand (4 seated players, win type, faan, etc.)
- `/leaderboard` — show total points, ranked
- `/stats [player]` — games played, win rate, avg winning faan, etc.
- `/recent-games` — last 10 games logged
- `/ping` — check the bot is alive

## Part 1: Create the Discord bot application

1. Go to https://discord.com/developers/applications and click **New Application**.
2. Name it (e.g. "Mahjong Bot"), then go to the **Bot** tab on the left.
3. Click **Reset Token** (or **Copy**) to get your bot token — save this somewhere
   safe, you'll need it below. Never share it publicly or commit it to GitHub.
4. Still on the Bot tab, make sure **Public Bot** is off if you don't want
   others adding it to their servers.
5. Go to the **OAuth2 → URL Generator** tab:
   - Under **Scopes**, check `bot` and `applications.commands`
   - Under **Bot Permissions**, check `Send Messages` and `Embed Links`
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
