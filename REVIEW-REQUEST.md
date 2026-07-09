# tgbridge v2 — second-review request

**For the reviewer (fable):** you wrote `IMPROVEMENTS.md`; this is the follow-up. The spec (phases 1–3 + 3.8) is now implemented, **plus four features added afterwards** that were not in the spec. Please review the actual code for correctness, concurrency safety, and anything the implementer missed. Focus your effort on **Part B** (the post-spec features), especially **B2 (concurrency)** — that's where the real risk is.

## How to review

- **One file:** `~/dev/tgbridge/bridge.py` (stdlib-only Python 3.9, single file). Helpers: `tgsend` (bash), `com.local.tgbridge.plist` (launchd).
- **Git:** repo at `~/dev/tgbridge`, baseline (v1, pre-changes) = `e234bc2`. Per-feature commits below. Working tree clean.
  - Whole diff: `git -C ~/dev/tgbridge diff e234bc2..HEAD -- bridge.py` (390 insertions / 81 deletions).
  - Or review commit-by-commit.
- **Runtime unchanged:** launchd `com.local.tgbridge` under anaconda base py3.9, log `~/Library/Logs/tgbridge.log`, persistent state `~/.config/tgbridge/state.json` (chmod 600). Reload = `launchctl unload/load`.
- **Invariants that must still hold:** stdlib-only; single file; responds ONLY to owner TG id **and** private chat; token never read/logged; agent runs `bypassPermissions`; `!bash` allowlist is a fat-finger guard, not a security boundary. Bot-facing strings are Russian by design; code/docs English.

### Commits

```
b7c3379 command menu via setMyCommands            (B4)
ba860f8 close side-chats on return to main        (B3)
8a5bc56 two lanes: main_lock vs side_lock         (B2)
c44f0f0 video_note (circles) handled like video   (B1)
21fa69f state.json chmod 600
c82185f Implement IMPROVEMENTS.md phases 1-3       (Part A)
e234bc2 baseline v1
```

---

## Part A — IMPROVEMENTS.md, as implemented (recap + deviations)

All of phases 1–3 + 3.8 landed (commit `c82185f`, plus `21fa69f`). Highlights and **deviations from the spec you should know about**:

- **Prompt via stdin** (1.7): verified the installed CLI (2.1.198) reads `-p` from stdin; `run_claude` feeds `input=prompt`, prompt removed from argv. Flag-injection and argv-limit both gone.
- **state.json** (2.1): persists `session_id / total_cost / offset / effort / btw` map, atomic (`os.replace`) under a `state_lock` (added — concurrent writers were racing the temp file), **chmod 600** (added, `21fa69f`, to match config.env).
- **Log** (2.3): moved to `~/Library/Logs/tgbridge.log`. **Auto-rotation NOT implemented** — rotating launchd's own stdout fd from inside the process is racy; the spec permitted skipping it. `incoming/` 30-day cleanup IS implemented. *Please sanity-check this decision.*
- **Send retries** (2.2): `tg()` now returns the parsed body on HTTPError (so `None` == transport failure only); `_tg_send_retry` retries transport failures (0/2/5/10 s), parse errors fall back to plain text.
- **Commits consolidated:** the spec asked for per-phase commits; implemented as baseline + one big commit + polish, because it was a coordinated single-file rewrite. Flagging the process deviation.

Everything else (chat_id gate, per-instance loop Event, edited-message freshness, /reset generation counter, socket timeout, tgsend `--form-string`, allowed_updates, /btw fork, /cancel, /effort, document albums, video→audio 3.8) implemented as specced.

---

## Part B — features added AFTER the spec (review these closely)

### B1 — video_note (Telegram "circles") routed like video · `c44f0f0`

**Why:** a circle (`video_note`) is delivered as `.mp4` but was falling into the whisper-direct **voice** branch → no ffmpeg extraction, and that branch never deletes its download, so circles piled up in `incoming/`.

**Change:** `video_note` now shares the **video** branch: `extract_audio()` (ffmpeg `-vn -c:a copy` → `.m4a`, fallback aac) → delete source `.mp4` → transcribe. Prompt marker distinguishes `[кружок (video_note)…]` vs `[видео…]`. Also **added failure logging** to `extract_audio` (ffmpeg stderr tail) and to the delete steps — the previous blanket `except: pass` was a genuine blind spot that made a live debug hard.

**Review focus:** branch ordering in `main()` (`voice/audio` vs `video/video_note`); the fallback where ffmpeg fails → whisper decodes the mp4 directly → source deleted *after* transcription (not before). Verified end-to-end on a real circle (m4a produced, mp4 gone, correct transcript).

### B2 — two lanes: `main_lock` vs `side_lock` (concurrency) · `8a5bc56` ⭐ HIGHEST RISK

**Why:** a single `claude_lock` serialized **every** agent turn. A busy main turn blocked `/btw` and reply-branches — the user hit "главный чат думает вечно" while a side-chat held the lock. (Contributing factors that day were also flaky network + a mid-conversation reload, but the serialization is real.)

**Change:** two independent locks —
- `main_lock` — main dialog turns (must serialize with each other: same `session_id` can't run twice).
- `side_lock` — `/btw`, reply-branches, **and** loop ticks.

Main and side now run **concurrently** (bounded to max 2 `claude` processes, since side-lane is internally serialized).

**Two concurrency edges handled (please verify I got these right):**
1. **`/cancel` with 2 possible in-flight turns:** `current_proc` (single) → `current_procs` (a `set`); `run_claude` adds/discards under `proc_lock`; `cancel_all()` kills **every** live process (process-group SIGTERM → SIGKILL after 5 s). `/cancel` now reports "Прервал текущие ходы (N)".
2. **`total_cost += ` from two threads:** all increments go through `add_cost(data)` under `cost_lock` (lost-update race otherwise).

`/status` now shows both lanes: `агент занят: главный=…, side=…`.

**Known caveat I want you to rule on:** `/btw` forks the main `session_id` (`--fork-session`) while a main turn may concurrently be resuming/writing that same session. Different session files → no corruption, but the fork may snapshot main's state a moment stale. I judged this acceptable for a personal bot and did **not** serialize the fork behind `main_lock` (that would defeat the point). **Is this the right call, or is there a clean mitigation?**

**Also rule on:** is "`/cancel` kills ALL lanes" the right semantics (vs. only the main lane)? And is putting loop on `side_lock` (so a loop tick and a `/btw` serialize, but both run parallel to main) a sound resource bound?

Verified (stubbed, no network/agent): `add_cost` under 50 threads → exact total (no race); `main_lock`/`side_lock` independence; `cancel_all` kills all live fakes, ignores dead. **Not** verified from a real phone under concurrent load — see checklist gap below.

### B3 — close all side-chats on return to the main dialog · `ba860f8`

**Why:** the side-chat routing map (`btw_threads`, message_id → forked session_id) persisted indefinitely (FIFO-capped at 200); no clean "I'm done with side-chats" lifecycle. (Note: the bot agent's claim that side-chats "hang forever eating resources" was overstated — they hold no process/lock, just a tiny map entry + on-disk session file. This is a UX/lifecycle change, not a resource fix.)

**Change:** any **normal main-dialog turn** clears `btw_threads` (all side-chats "close"); one-line notice `🗂 Закрыл боковые чаты (N)`. A **reply to a 💬** stays in the side; **commands** (`/status`, `/cancel`, …), `/btw`, and `!bash` do **NOT** close (they early-return before the clear, or are routed away before `handle()`). A reply to an already-closed 💬 gracefully becomes a main-chat message with quote-context (not an error).

**Review focus:** placement of the clear in `handle()` — it sits after all command early-returns, so only a genuine main turn reaches it. Confirm no command path falls through to it. Is clearing on *every* main turn too aggressive, or correct for the "excursion" model? "Closing" clears only the routing map; on-disk forked session files are left to Claude Code. Verified with a stub test (main closes, `/status` doesn't, `/btw` doesn't).

### B4 — Telegram command menu via `setMyCommands` · `b7c3379`

Registers 9 commands + descriptions at startup (after `deleteWebhook`) so the `/` autocomplete and Menu button populate. Verified live via `getMyCommands` (Telegram returned all 9). Low risk; just eyeball `BOT_COMMANDS` for wording/coverage (`/start` intentionally omitted as a `/help` duplicate).

---

## Verification status (so you know what's proven vs not)

- **Static:** `py_compile` (anaconda 3.9), `bash -n tgsend`, `plutil` plist, `tgsend --check` — all pass.
- **Logic (stubbed / live, no phone):** `md_to_html`, `_chunks`, state.json round-trip, btw FIFO cap, `add_cost` race, lock independence, `cancel_all`, side-close behavior; **live:** `--fork-session` (sees context, returns new id), full video_note pipeline.
- **Reloads:** each done only after checking no in-flight turn; clean startup + stable PID + session survived each time.
- **NOT yet verified end-to-end from a phone:** concurrency under real load (main ∥ side), `/cancel` killing 2 real turns, reply-branch UX, video/circle from the actual Telegram client. A phone checklist exists but hasn't been fully run.

## Open questions for the reviewer (ranked)

1. **B2 fork-during-main-write race** — accept, or mitigate (and how, without re-serializing)?
2. **B2 `/cancel` kills all lanes** — right semantics for a single-user bot?
3. **B3 clear-on-every-main-turn** — too aggressive? Should `/compact` (a main-dialog passthrough) really close side-chats?
4. **A/2.3 log rotation skipped** — acceptable, or is there a safe in-process approach given launchd owns the fd?
5. **`session_id`/`last_data` single globals** — only the main lane writes them, `/reset` uses the `reset_gen` guard; confirm the two-lane change opened no new hole.
6. **On-disk forked sessions accumulate** — worth a cleanup pass, or leave to Claude Code?

## Files
- `bridge.py` — all logic (review target).
- `tgsend` — curl file-sender (`--form-string` fix).
- `~/Library/LaunchAgents/com.local.tgbridge.plist` — launchd (log path).
- `~/.config/tgbridge/config.env` — secrets, **not in repo**, do not request its contents.
- `IMPROVEMENTS.md` — the original spec (Part A).
