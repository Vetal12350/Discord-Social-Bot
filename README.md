# Discord Social Bot

[![XERJ Corpus Hub contributor](https://hub.xerj.org/contributors/Vetal12350/badge.svg)](https://hub.xerj.org/contributors/Vetal12350)

**Languages:** [English](README.md) · [Русский / Russian](README.ru.md)

A self-hosted, open-source Discord bot that reposts supported **X/Twitter** and **TikTok** links with media, optional Russian translation and configurable webhook reposting.

**Quick links:** [Русская документация](README.ru.md) · [Discord setup (RU)](docs/DISCORD_SETUP_RU.md) · [Ubuntu VPS / systemd (RU)](docs/VPS_RU.md) · [Windows (RU)](docs/WINDOWS_RU.md) · [Altare (RU)](ALTARE.md) · [Troubleshooting (RU)](docs/TROUBLESHOOTING_RU.md)

## Features

- Reposts supported public X/Twitter and TikTok links to Discord. Known X embed aliases include `fixvx.com`, `vxtwitter.com`, `fxtwitter.com`, `fixupx.com`, `twittpr.com` and legacy `pxtwitter.com`.
- Downloads supported images and compatible MP4 without unnecessary transcoding.
- Attempts to convert short animations to animated WebP/GIF via FFmpeg.
- Optionally translates post captions into Russian through external translation services; preserves original content if translation is unavailable.
- In `webhook` mode, reposts with the message author's display name/avatar; deletes a **single-link-only** original message only after a confirmed successful repost and safety checks.
- Alternative `reply` mode posts as the bot without webhook sender styling.
- `/video` slash command attempts an MP4 download from a supported public URL.
- Bounded media processing, temporary disk cache, channel allowlist and resource-aware throttling.

> Media availability depends on third-party sites and may change. This is an unofficial community project, not affiliated with Discord, X, TikTok or Altare. It cannot guarantee successful reposting of every link.

## 1. Create the Discord application

1. Open the [Discord Developer Portal](https://discord.com/developers/applications) and create a new application.
2. Open **Bot**, create/retrieve the bot token and keep it secret. Enable **Message Content Intent** in *Privileged Gateway Intents*.
3. Configure installation/invite with scopes **`bot`** and **`applications.commands`**.
4. Grant only the channel permissions you need: View Channels, Send Messages, Read Message History, Embed Links and Attach Files. **Manage Webhooks** is required for `REPOST_MODE=webhook`; **Manage Messages** is needed only for deleting original messages. For threads, grant Send Messages in Threads. Administrator is not required.
5. Invite the bot to your Discord server and check permissions on the destination text channel.

For detailed illustrated-menu instructions and server/channel IDs, see [Discord setup (Russian)](docs/DISCORD_SETUP_RU.md).

## 2. Requirements

- Python **3.13 or 3.14** recommended; Python **3.12** was also verified on Ubuntu 24.04 with the pinned dependencies.
- `ffmpeg` and `ffprobe` installed and available on `PATH` for animation conversion.
- Discord bot token; ability to read messages and upload attachments in your chosen server/channel.

## 3. Install

Clone this repository and set up an isolated Python environment:

```bash
git clone https://github.com/Vetal12350/Discord-Social-Bot.git
cd Discord-Social-Bot
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
cp .env.example .env
```

These commands are for Linux/macOS with a suitable `python3`. For **Windows PowerShell**, see [Windows setup](docs/WINDOWS_RU.md). For a **production Ubuntu VPS with a dedicated service user, systemd auto-start, logs and safe update procedures**, see [VPS guide](docs/VPS_RU.md). For **Altare**, use [ALTARE.md](ALTARE.md) rather than systemd.

Open the local `.env` file and replace the placeholder with your actual token:

```dotenv
DISCORD_TOKEN=PASTE_YOUR_TOKEN_HERE
REPOST_MODE=webhook
TRANSLATE_ENABLED=true
TEST_GUILD_ID=
ALLOWED_CHANNEL_IDS=
MYMEMORY_EMAIL=
```

Test the runtime **without connecting to Discord**:

```bash
python check_runtime.py
python -m unittest discover -s tests -v
```

Start the bot:

```bash
python bot.py
```

Look for `Connected to Discord` in the logs, then try a public X/Twitter post and a TikTok post in a private test channel. Test GIF/WebP handling separately: the preflight checks installed codecs, but does not prove a particular social post will download.

**During migration, stop the old running instance before starting the new one with the same token.**

## Configuration

| Variable | Description |
| --- | --- |
| `DISCORD_TOKEN` | **Required** bot token from Discord Developer Portal. Legacy `DISCORD_BOT_TOKEN` is supported; use one. |
| `REPOST_MODE` | `webhook` (default) or `reply`. |
| `TRANSLATE_ENABLED` | `true` / `false` — attempt to translate captions to Russian. |
| `TEST_GUILD_ID` | Optional Discord server ID for faster guild-scoped slash-command sync; blank uses global sync. |
| `ALLOWED_CHANNEL_IDS` | Optional comma-separated channel IDs. Empty = all accessible channels. |
| `MYMEMORY_EMAIL` | Optional contact email for MyMemory translation service limits; not an API password. |

The example configuration is in [`.env.example`](.env.example). Do not commit your real `.env` or reveal tokens, webhook credentials, or private logs.

## Behavior and limitations

- In webhook mode the bot may replace a message containing **only one supported URL**, but only after the repost is confirmed and the original is safe to remove. Messages with a comment or other content are retained.
- When video, animation or translation fails, the bot may preserve the original link/content rather than deleting it.
- Large attachments may exceed both Discord upload allowances and the bot's own media budget.
- The bot needs no inbound HTTP port or public web server. Windows operation lasts only while the Python process/PC is running; systemd on VPS provides restart on failure.

See [troubleshooting (Russian)](docs/TROUBLESHOOTING_RU.md) for permissions, FFmpeg, token, synchronization, systemd and throttling issues.

## Development and security

- Source and docs are public; real tokens and private environment files must stay local.
- Run under a separate restricted OS account on a VPS and use least-privilege Discord permissions.
- The included GitHub Actions workflow checks syntax, imports and existing URL parsing tests. These are not live end-to-end Discord integration tests.
- The working Altare animation subprocess fix is included in `animation.py` and `media.py`.

See [CONTRIBUTING.md](CONTRIBUTING.md) to report issues or propose changes. Issue templates are available for bug reports and feature requests.

## License

Licensed under the [MIT License](LICENSE). Copyright (c) 2026 [Vetal12350](https://github.com/Vetal12350). Third-party components and platforms retain their own terms.
