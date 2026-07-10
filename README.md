# tgbridge

**Your phone → Telegram bot → headless Claude Code on your machine.**

A single-file, stdlib-only Python bridge that turns a personal Telegram bot into a full
Claude Code agent with access to your computer: bash, ssh, files, the works. Message the
bot from anywhere — the agent runs on your laptop and replies to Telegram.

```
┌────────┐   long-poll    ┌───────────┐   claude -p --resume   ┌──────────────┐
│ phone  │ ─────────────▶ │ bridge.py │ ─────────────────────▶ │ Claude Code  │
│ (TG)   │ ◀───────────── │ (launchd) │ ◀───────────────────── │  (headless)  │
└────────┘   sendMessage  └───────────┘      JSON result       └──────────────┘
```

No frameworks, no pip dependencies for the core — `bridge.py` runs on bare Python 3.9+.
Voice/video transcription is the only optional extra (whisper + ffmpeg).

## Features

**Conversation**
- Stateful dialog via `--resume`; survives bridge restarts (`state.json` persists session, offset, cost, side-chats).
- Markdown → Telegram HTML (bold, strike, code, links) with plain-text fallback and send retries.
- Long answers (> ~6000 chars) delivered as a `.md` file with a short summary.
- `/compact` compresses the dialog and reports honest **before → after** context size
  (measured by a cheap probe turn — the compact turn's own usage lies).

**Side-chats (`/btw`)**
- Real side conversations: fork the main session (`--fork-session`) — the side chat *sees*
  your dialog but never pollutes it.
- Continue a side-chat by **replying** to the bot's 💬 message; several parallel branches work.
- Writing a normal message to the main dialog closes all side-chats.

**Media in**
- Photos, documents, albums (media groups buffered into a single agent turn).
- Voice/audio → local whisper transcription, then executed as a command.
- Video and video-notes (circles) → ffmpeg extracts the audio track (video file deleted),
  whisper transcribes; the visual stream is deliberately not analyzed.

**Media out** — the agent sends files to your chat via the bundled `tgsend` helper.

**Control**
- Two execution lanes: a slow side-chat/loop never blocks the main dialog (max 2 concurrent agents).
- `/cancel` kills all in-flight agent turns (process groups — no orphaned children).
- `/effort low…max` switches reasoning effort on the fly; `/loop N <prompt>` repeats a prompt.
- `!cmd` — quick allowlisted bash (jupyter-style) without waking the agent.
- Native Telegram command menu (`setMyCommands`).

## Commands

| Command | Description |
|---|---|
| `/reset` | start a new dialog |
| `/status` | bridge status: session, cost, lanes, queued jobs, side-chats |
| `/context` | context usage + cost of the last turn |
| `/compact` | compress the dialog context, show before → after |
| `/cancel` | abort all running agent turns (and the loop) |
| `/effort low\|medium\|high\|xhigh\|max` | reasoning effort on the fly |
| `/btw <question>` | side-chat that sees the dialog but doesn't touch it |
| `/loop <sec> <prompt>` | repeat a prompt every N seconds (`/loop stop`) |
| `!<cmd>` | quick bash from the allowlist (no `rm`/`mv`/`sudo`/…) |

Anything else — plain text, voice, photo, file — goes to the agent.

## Setup

1. **Create a bot**: [@BotFather](https://t.me/BotFather) → `/newbot` → token.
   Get your numeric Telegram id (e.g. from [@userinfobot](https://t.me/userinfobot)).
2. **Install Claude Code** and log in once interactively: `claude` (the bridge uses your
   subscription via the CLI; `total_cost_usd` in `/context` is an API-price estimate, not a bill).
3. **Config**: copy [`config.env.example`](config.env.example) to `~/.config/tgbridge/config.env`,
   fill `TG_BOT_TOKEN` and `TG_ALLOWED_ID`, then `chmod 600` the file (and `chmod 700` the directory).
4. **Voice/video (optional)**: `poetry install --extras voice` (or any env with `openai-whisper`),
   make sure `ffmpeg` sits next to the `whisper` binary or set `FFMPEG_BIN`. First voice message
   downloads the model (~0.5 GB for `small`).
5. **Autostart**:
   - macOS: copy [`com.local.tgbridge.plist.example`](com.local.tgbridge.plist.example) to
     `~/Library/LaunchAgents/com.local.tgbridge.plist`, fix the paths, then
     `launchctl load -w ~/Library/LaunchAgents/com.local.tgbridge.plist`.
   - Linux: a systemd unit with `ExecStart=<python3> <repo>/bridge.py`, `Restart=always`.

Logs: `~/Library/Logs/tgbridge.log`. Reload after editing the code:
`launchctl unload … && launchctl load -w …` — the dialog survives (state is persisted).

⚠️ Paths `INCOMING`/`OUTGOING`/`TGSEND` are anchored to `~/dev/tgbridge/` — keep the repo
there or adjust the constants at the top of `bridge.py`.

## Security model (read this)

- **The only gate is Telegram identity**: the bridge reacts solely to messages where both
  `from.id` and `chat.id` equal `TG_ALLOWED_ID`. Telegram sets `from.id` server-side — it
  cannot be spoofed by other users; a leaked bot token alone lets nobody command your agent
  (it would let them read bot updates, so still keep it secret and rotate if leaked).
- **The agent runs with `bypassPermissions`** on your machine. Anyone who controls your
  Telegram account controls the agent. Protect the account (2FA), not just the token.
- The bot token lives in `~/.config/tgbridge/config.env` (600), never in code, argv, env or logs.
  `state.json` is chmod 600 too.
- **`!bash` allowlist is a fat-finger guard, not a security boundary** — `python`/`git`/`find`
  can escape it by design; the boundary is the identity gate above.
- Prompt-injection hygiene: the system prompt instructs the agent to treat forwarded messages
  and file contents as data, not instructions; forwards are explicitly labeled.
- Delivery is at-most-once: a message consumed by the bridge right before a crash/restart may
  be lost. Check `задач у агента` in `/status` before restarting the bridge.

## Repo layout

| File | Purpose |
|---|---|
| `bridge.py` | the whole bridge (single file, stdlib only) |
| `tgsend` | bash+curl helper: send a file to the owner's chat |
| `config.env.example` | config template → `~/.config/tgbridge/config.env` |
| `com.local.tgbridge.plist.example` | launchd template (macOS autostart) |
| `pyproject.toml` / `poetry.lock` | packaging; optional `voice` extra pins whisper+torch |
| `IMPROVEMENTS.md` | v2 spec the implementation was built from |
| `REVIEW-REQUEST.md` | reviewer handoff: architecture, invariants, open questions |
| `CODEX_REVIEW.md` | third-party review (Codex), findings addressed in git history |

## Development history

Built and hardened in one day by a human + three models tag-teaming:
spec & two review passes by **Claude Fable 5**, implementation by **Claude Opus 4.8**,
independent third review by **Codex** — every finding either fixed with a regression test
or explicitly declined with rationale (see commit messages). The git log tells the story.
