# CODEX_REVIEW.md

Дата ревью: 2026-07-09  
Ревьюер: Codex  
Проект: `tgbridge` — личный Telegram -> Claude Code bridge  
Scope: `bridge.py`, `tgsend`, `REVIEW-REQUEST.md`, `IMPROVEMENTS.md`, git diff
`e234bc2..HEAD`.

Важно: конфиг `~/.config/tgbridge/` не читался, `incoming/` и `outgoing/` не
просматривались. Код и существующие файлы проекта не изменялись; добавлен только этот
review-файл по просьбе владельца.

## Executive Summary

Общий вывод: v2 выглядит здраво и заметно надежнее baseline. Главные инварианты
сохраняются: stdlib-only, single-file, private-chat gate по `from.id` и `chat.id`,
prompt идет в Claude через stdin, token не логируется, `/cancel` убивает process group,
а две lanes (`main_lock` и `side_lock`) действительно снимают старую проблему полной
сериализации main и `/btw`.

Критичных security-багов уровня "чужой пользователь может достучаться" я не нашел.
Но после B2 появились реальные concurrency/UX края вокруг побочных состояний, которые
не защищены теми же lock-инвариантами, что Claude-процессы. Перед активным использованием
я бы закрыл два P1:

1. collision имен файлов длинных ответов при одновременном завершении main и side;
2. race, где "закрытие side-чатов" может быть отменено поздним side-ответом.

Остальное — P2/P3: at-most-once ack offset, общий `bash_cwd`, cleanup `outgoing/`,
long-answer side UX, chmod state на старте.

## Verification Performed

- `bash -n tgsend` — OK.
- `git status --short` — clean.
- Прочитан `REVIEW-REQUEST.md`.
- Прочитан `IMPROVEMENTS.md`.
- Просмотрены ключевые участки `bridge.py` и diff `e234bc2..HEAD`.
- Конфиг с токеном не открывался.
- Личные файлы в `incoming/` / `outgoing/` не открывались.

Не выполнялось:

- live Telegram phone checklist;
- реальные Claude turns;
- чтение launchd plist вне репозитория;
- чтение `~/.config/tgbridge/config.env` или state.

## Findings

### P1. Long-answer file names can collide across lanes

Location: `bridge.py:242-255`

```python
if len(text) > LONG_LIMIT:
    path = os.path.join(OUTGOING, f"answer_{int(time.time())}.md")
```

After B2, main and side can finish in parallel. If both produce long answers in the
same second, they write the same `outgoing/answer_<sec>.md`. The second write can
overwrite the first before `tgsend` reads it, so the owner may receive the wrong file
or two messages pointing to one answer.

This is a real regression introduced by the successful two-lane model. Before B2,
the single Claude lock made simultaneous `send_reply()` for agent results much less
likely.

Recommendation:

- Use a unique temporary file name:
  - `tempfile.NamedTemporaryFile(delete=False, dir=OUTGOING, suffix=".md")`;
  - or `answer_{time.time_ns()}_{threading.get_ident()}.md`.
- Prefer `tempfile` because it also makes future atomic-write behavior simpler.

Suggested acceptance check:

- Stub `send_reply()` or force two threads to call it with `LONG_LIMIT+` text in the
  same second; verify two distinct files are created and both `tgsend` calls get the
  intended content.

### P1. Closing side-chats can be undone by an in-flight side response

Locations:

- side registration: `bridge.py:532-541`, `bridge.py:543-552`
- `/btw` registration: `bridge.py:598-606`
- side clear on main return: `bridge.py:609-615`

Current behavior:

1. Owner starts a `/btw` or replies to a side thread.
2. Side lane is running.
3. Owner sends a normal main message.
4. Main handler clears `btw_threads` and sends `🗂 Закрыл боковые чаты (N).`
5. The earlier side turn finishes later and calls `register_btw(ids, sid)`.
6. Side thread is alive again despite the owner having "closed" it.

That is surprising UX: the bot explicitly says side chats were closed, but a late
`💬` answer can resurrect routing.

Recommendation:

- Add a monotonic `side_epoch` / `btw_generation`.
- Each side turn captures the epoch before `run_claude`.
- Main clear increments the epoch.
- `register_btw` accepts `expected_epoch` and only registers if it still matches.

Sketch:

```python
side_epoch = 0

def close_sides():
    global side_epoch
    with state_lock:
        n = len(btw_threads)
        btw_threads.clear()
        side_epoch += 1
    save_state()
    return n

def current_side_epoch():
    with state_lock:
        return side_epoch

def register_btw(ids, sid, expected_epoch=None):
    if not sid or not ids:
        return
    with state_lock:
        if expected_epoch is not None and expected_epoch != side_epoch:
            return
        ...
```

For `/btw` and `handle_side`, capture `epoch = current_side_epoch()` at the beginning
of the turn and pass it into `register_btw`.

Suggested acceptance check:

- Stub a slow side turn.
- Send a main message that clears side chats before side completes.
- Let side complete.
- Verify its returned message ids are not added to `btw_threads`.

### P2. Offset is acknowledged before worker completion

Locations:

- worker thread spawn: `bridge.py:813`
- offset advance/save: `bridge.py:832-839`

`process_update()` starts a daemon worker and returns immediately. The polling loop then
saves `offset` for the whole batch. If the process dies, is reloaded, or the Mac reboots
while a spawned job is still waiting on `main_lock` or running Claude, Telegram will not
redeliver that update.

This is an at-most-once delivery model. It may be acceptable for a personal bridge, but
it should be explicitly accepted because persistent `offset` can make "message was
accepted by bridge process" look like "message was fully handled".

Risk examples:

- main turn A is running;
- message B arrives and spawns a worker blocked on `main_lock`;
- polling loop saves offset for B;
- bridge is reloaded before B enters `run_claude`;
- B is lost.

Recommendation options:

1. Keep current behavior, but document "at-most-once after poll acceptance".
2. Lightweight mitigation: track active/queued job counters and show them in `/status`,
   so owner can avoid reload while work is pending.
3. Stronger mitigation: replace immediate thread spawn with an in-process queue and
   save offset only after job completion. This is a larger design change and may not be
   worth it for this personal bridge.

My recommendation: option 2 is enough unless this bridge starts doing high-value tasks
where losing a Telegram message is unacceptable.

### P2. `!` command cwd has a race

Location: `bridge.py:489-526`

`handle_bang()` runs in its own thread for each `!` command and reads/writes the global
`bash_cwd` without a lock. A quick sequence like:

```text
!cd ~/dev/project-a
!ls
!cd ~/dev/project-b
!pwd
```

can interleave in surprising ways. This is not a security boundary problem because the
project already accepts that `!bash` is a convenience/fat-finger guard, not hardening.
But it is a correctness/UX issue.

Recommendation:

- Add a `bash_lock`.
- Easiest: wrap all of `handle_bang()` in `with bash_lock`.
- This serializes `!` commands and makes `bash_cwd` behave like one tiny shell session.

Suggested acceptance check:

- Fire multiple `!cd` / `!pwd` calls concurrently in a small stub; verify order is
  deterministic or at least no partial cwd race occurs.

### P2. `outgoing/` accumulates sensitive long answers

Location: `bridge.py:242-255`

`incoming/` has a 30-day cleanup, but long answers written to `outgoing/answer_*.md`
are not cleaned up. The owner specifically called out that `outgoing/` can contain
agent responses, so it should be treated as sensitive local data too.

Recommendation:

- Delete long-answer file after successful `tgsend`, if persistence is not useful.
- Or extend cleanup to `outgoing/answer_*.md` older than N days.
- If debugging long answers is useful, choose a short retention window.

Follow-up note:

- If the filename-collision P1 is fixed using `tempfile`, cleanup becomes more important
  because names will be less predictable.

### P2. Long side answers are only continuable via the notice message, not the file

Locations:

- long answer path: `bridge.py:242-252`
- side registration calls: `bridge.py:551-552`, `bridge.py:605-606`
- `tgsend`: `tgsend:17-25`

For long answers, `send_reply()` sends a short notice through `send()`, then sends the
actual file through `tgsend`. It returns ids only for the notice message. `tgsend` does
not return the Telegram `message_id` of the uploaded document.

In side-chat UX, this means replying to the short "📄 Ответ длинный..." notice continues
the side thread, but replying to the actual document/file probably does not. This is not
data loss, but it is a likely user surprise.

Recommendation options:

1. Accept and maybe phrase the notice: "ответь на это сообщение, чтобы продолжить".
2. Teach `tgsend` to print Telegram JSON / message id and have `send_reply()` register
   the document message as well.
3. For long side answers, avoid `tgsend` helper and upload document from Python so ids
   are available directly. This is more code.

My recommendation: option 1 is enough for now; option 2 if side chats with long answers
become common.

### P3. Existing `state.json` permissions are not enforced on load

Locations:

- `save_state()`: `bridge.py:132-143`
- `load_state()`: `bridge.py:145-164`

New writes chmod the temp file to `0600`, which is good. But if an older `state.json`
already exists with loose permissions, startup only reads it and does not fix mode until
the next save.

Recommendation:

- After successful load, try `os.chmod(STATE_FILE, 0o600)`.
- This mirrors the explicit chmod behavior for new saves.

Severity is low because state contains session ids and side routing, not the Telegram
token, and any normal bridge activity will soon rewrite it with 0600.

### P3. Incoming download filenames can collide in narrow cases

Location: `bridge.py:270-284`

Download destination uses `int(time.time())` and the last 8 chars of `file_id`. This is
usually fine. It can collide if the same file id is downloaded twice within the same
second, or if Telegram ids share the suffix.

Recommendation:

- Same direction as P1: use `time.time_ns()` or `tempfile` in `INCOMING`.
- Low priority unless duplicate file replies become common.

## Answers to REVIEW-REQUEST Open Questions

### 1. B2 fork-during-main-write race

My call: acceptable.

`/btw` fork while main is writing/resuming the same session can snapshot a slightly stale
main session. For a personal bridge this is a reasonable trade-off: `/btw` stays useful
because it does not wait behind a long main turn.

I would document the semantics as:

> `/btw` sees the latest committed Claude session at the moment the side turn starts,
> not necessarily in-flight main work.

Clean mitigation without defeating the feature:

- optional "strict fork" mode only if needed;
- if `main_lock.locked()`, send a tiny notice "side sees last completed main turn";
- do not serialize `/btw` behind `main_lock` by default.

### 2. `/cancel` kills all lanes

My call: correct for a single-user bot.

The owner is interacting with one personal bridge, not a multi-user job runner. If they
type `/cancel`, the intuitive meaning is "stop whatever is currently burning my laptop
or blocking me". Counting killed processes in the reply is good.

If lane-specific cancel is ever needed, add new commands later:

- `/cancel main`
- `/cancel side`

But default `/cancel` should keep killing all live Claude processes.

### 3. Loop on `side_lock`

My call: sound resource bound.

This gives max two Claude processes:

- one main;
- one side-class process (`/btw`, side reply, or loop tick).

It prevents loop and `/btw` from piling up together while still fixing the original pain:
side work no longer freezes the main dialog.

### 4. B3 clear-on-every-main-turn

My call: correct for the "excursion" model, with the epoch fix above.

Normal main text means "I am back in the main conversation", so closing side routes is
reasonable and keeps the mental model simple.

The current exception for slash passthrough (`not t.startswith("/")`) is good. It means
`/status`, `/cancel`, `/compact`, etc. do not implicitly close side chats.

One nuance: if `/compact` is intended as a main-dialog mutation, not a command/status
operation, then leaving side chats open after `/compact` is still acceptable because side
sessions are forks/snapshots anyway. No need to special-case unless owner finds it weird.

### 5. Log rotation skipped

My call: acceptable to skip in-process rotation.

Because launchd owns stdout/stderr file descriptors, rotating from inside the Python
process is easy to get wrong. A safer approach is external rotation:

- `newsyslog` rule;
- launchd/cron cleanup job;
- manual truncation during maintenance.

The important improvement already landed: log moved out of `/tmp`.

### 6. `session_id` / `last_data` single globals

My call: acceptable with current invariant: only main lane writes them.

B2 did not appear to introduce a direct data race here because side code does not update
`session_id` or `last_data`. `/reset` generation guard also addresses the main in-flight
writeback case.

Remaining nuance:

- messages queued before `/reset` but executed after `/reset` may run in the fresh
  session because `gen` is captured inside `main_lock`.
- That is consistent with the current at-most-once/threaded model, but worth remembering.

### 7. On-disk forked sessions accumulate

My call: leave to Claude Code for now.

The app only keeps a capped Telegram `message_id -> side_session_id` map. The actual
Claude session storage is managed by Claude Code. Cleaning that from tgbridge risks
coupling to undocumented internals.

If disk usage becomes real, add a separate maintenance command/report first, not silent
deletion.

## Notes on Things That Look Good

- Private-chat gate checks both `from.id` and `chat.id`: `bridge.py:729-734`.
- Fresh-edit policy avoids stale edited messages retriggering old tasks:
  `bridge.py:735-737`.
- Prompt goes through stdin to `claude -p`, avoiding argv flag injection and argv length
  limits: `bridge.py:359-376`.
- Claude process is started in a new process group and `/cancel` kills the group:
  `bridge.py:330-350`, `bridge.py:359-388`.
- `current_procs` set is the right shape after two-lane concurrency.
- `add_cost()` closes the obvious lost-update race in `total_cost +=`:
  `bridge.py:353-357`.
- Side and main lanes are separated exactly where expected:
  `bridge.py:545-547`, `bridge.py:598-600`, `bridge.py:616-619`.
- `register_btw()` mutation and `save_state()` iteration are now coordinated by
  `state_lock`: `bridge.py:532-541`, `bridge.py:132-143`.
- `tgsend` switched text fields to `--form-string`: `tgsend:17-19`.
- Video notes share the video path and clean up the heavy `.mp4` when possible:
  `bridge.py:781-806`.

## Suggested Patch Order

1. Fix unique filenames for long answers (`send_reply`).
2. Add side epoch/generation guard for clear-vs-late-side-registration.
3. Add `bash_lock` for `!` commands.
4. Add cleanup/delete behavior for `outgoing/answer_*.md`.
5. Decide/document offset at-most-once semantics or add `/status` active/queued counters.
6. Optionally chmod existing `state.json` on load.

## Suggested Phone Checklist After Fixes

1. Main long-running turn + `/btw` in parallel: both complete, main session remains main.
2. `/cancel` while main and side are both running: both stop, no stray `claude`/child
   processes remain.
3. `/btw` slow answer, then normal main message before side completes: side should not
   resurrect after the clear if epoch fix is applied.
4. Two long answers finishing in the same second: two distinct files delivered.
5. Reply to a side message continues side; normal message closes side; slash command does
   not close side.
6. Video note from Telegram client: `.mp4` does not pile up, transcript reaches Claude.
7. Restart bridge while no active jobs; state/session/offset survive.
8. Optional harsh test: restart bridge while a job is queued/running to confirm accepted
   at-most-once behavior is understood.

## Final Recommendation

I would ship this for personal use after closing the two P1 items. The architecture is
still intentionally simple, and that is a strength here. The main thing is to make the
new two-lane world consistent for all shared side effects, not only for Claude process
execution.

