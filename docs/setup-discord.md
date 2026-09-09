# Discord setup

## 1. Create the application and bot

1. Open https://discord.com/developers/applications and click **New Application**. Name it (this is
   the bot's display name).
2. In the left menu open **Bot**. Click **Reset Token**, copy the token into `.env` as
   `DISCORD_TOKEN`. You cannot view it again later; reset it if lost.
3. Still under **Bot → Privileged Gateway Intents**, enable **Message Content Intent**. Without it
   Discord blanks message text and attachments for bots in servers. (Server Members and Presence
   are not needed.)
4. Optional: under **Bot**, turn off **Public Bot** so only you can add it to servers.

## 2. Invite it to your server

1. Open **OAuth2 → URL Generator**.
2. Scopes: `bot` and `applications.commands`.
3. Bot permissions: **View Channels, Send Messages, Send Messages in Threads, Read Message History,
   Add Reactions, Embed Links**. (This is permission integer `277025508416`; no admin needed.)
4. Open the generated URL, pick your server, authorize.

If you later add channels the bot cannot see, give its role access in the channel's permission
settings (it needs *View Channel* and *Read Message History*; *Manage Threads* only if you want private
archived threads archived).

## 3. Collect the IDs for config.yaml

1. Discord → User Settings → **Advanced** → enable **Developer Mode**.
2. Right-click your server icon → **Copy Server ID** → `guild_id`.
3. Right-click each channel → **Copy Channel ID** → `watched_channels` / `archived_channels`.
4. Right-click a user → **Copy User ID** → `allowed_user_ids` and the `people` map.
5. Right-click a role (Server Settings → Roles) → **Copy Role ID** → `allowed_role_ids`.

A channel can be both watched and archived.

## 4. First run checklist

- `docker compose up -d --build` (or `python -m dob run`) and watch the logs for
  `logged in as <bot> (guild <id>)`.
- Slash commands are registered to your guild on startup and appear immediately (global commands
  would take up to an hour).
- Drop a small `.md` file in a watched channel: you should see ⏳ then ✅ within seconds.
- Post a YouTube link in a watched channel: the bot downloads the audio and transcribes it.
- Run `/status` to confirm the queue and ledger are visible to you (you must be in
  `allowed_user_ids`, hold an allowed role, or be a server administrator).
- Run `/backfill` to archive channel history. Large servers take a while: Discord serves 100
  messages per request and the bot pauses a second between pages.

## Upload limits

Discord caps uploads at 20 MiB for free servers (50 MB at boost level 2, 100 MB at level 3; Nitro
raises the poster's own cap). For anything bigger, post a link (YouTube, Loom, Google Drive share
link) or put the file in the bot's `inbox/` folder.
