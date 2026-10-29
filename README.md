# Discord Social Bot

A self-hosted Discord bot that reposts supported social media links with downloadable media, with an optional webhook mode. Built for Python and tested in an Altare deployment.

## Features

- Reposts supported X/Twitter and TikTok content to Discord.
- Sends compatible MP4 files without unnecessary transcoding.
- Processes short animations into Discord-friendly animated WebP/GIF where possible; leaves the original link when conversion fails.
- Optional translation of post text into Russian (via an external translation service).
- Optional webhook reposting; deletes the original message only after a confirmed successful repost and when safe.
- Disk-based temporary media cache, streaming transfers, bounded media processing.
- Optional channel allowlist.

**Note:** Media availability depends on third-party sites and their changing interfaces. This project is not affiliated with Discord, X, TikTok, or Altare.

## Requirements

- Python 3.13 or 3.14
- `ffmpeg` and `ffprobe` on `PATH` (needed for animation conversion)
- A Discord bot token and the appropriate permissions in your server

## Setup

```bash
python -m venv .venv
# Linux/macOS
source .venv/bin/activate
# Windows: .venv\\Scripts\\activate
python -m pip install -r requirements.txt
cp .env.example .env
```

Edit `.env` locally to provide `DISCORD_TOKEN`. Never commit this file or paste a token into a public issue.

Run the optional preflight:

```bash
python check_runtime.py
```

Start the bot:

```bash
python bot.py
```

### Discord permissions

Grant permissions required by your selected mode, including View Channel, Send Messages, Embed Links and Attach Files. Webhook repost mode also needs Manage Webhooks; deletion of original messages requires Manage Messages. Configure Discord message-content intent if needed by the bot's message handling.

### Altare deployment

See [ALTARE.md](ALTARE.md) for the Altare-specific guide. Set `PY_FILE=bot.py` and `REQUIREMENTS_FILE=requirements.txt` in the panel's Startup variables when using its Python template. Store `DISCORD_TOKEN` only in the provider's private environment or a private `.env` file.

Do not run two instances using the same bot token while testing a migration.

## Configuration

Copy `.env.example` for the supported variables:

| Variable | Purpose |
| --- | --- |
| `DISCORD_TOKEN` | Required Discord bot token (`DISCORD_BOT_TOKEN` also supported) |
| `TEST_GUILD_ID` | Optional guild ID for command sync |
| `ALLOWED_CHANNEL_IDS` | Optional comma-separated allowlist |
| `REPOST_MODE` | `webhook` or `reply` |
| `TRANSLATE_ENABLED` | Enable/disable translation |
| `MYMEMORY_EMAIL` | Optional contact email for external translation quota |

## Development and safety

The repository contains no real tokens or `.env` file. The bot handles user-supplied URLs and media; run it with the least privileges necessary and keep dependencies updated. CI checks Python syntax and imports; it is not an end-to-end Discord integration test.

The working Altare GIF fix is included in `animation.py` and `media.py`. Tests of live Discord/Altare behavior should be performed in a private test channel before production changes.

## License

No open-source license has been selected yet. Public visibility alone does **not** grant permission to reuse, modify or redistribute the code. Choose and add a license after reviewing any third-party code and dependencies.
