#!/usr/bin/env python3
"""
tgbridge — Telegram <-> Claude Code (headless) bridge.

Ты пишешь боту -> проверка твоего TG id -> headless `claude -p` (stateful) -> ответ назад.
Отвечает ТОЛЬКО на сообщения от TG_ALLOWED_ID в личном чате. Только stdlib, без pip-зависимостей.

Конфиг: ~/.config/tgbridge/config.env  (chmod 600)
    TG_BOT_TOKEN=...            (обязательно)
    TG_ALLOWED_ID=123456789     (обязательно; твой Telegram user id)
    WORKDIR=~/dev               (необязательно; рабочая папка агента, по умолч. HOME)
    CLAUDE_BIN=~/.local/bin/claude
    PERMISSION_MODE=bypassPermissions
    EFFORT=high                 (уровень reasoning: low|medium|high|xhigh|max; можно менять /effort)
    CLAUDE_MODEL=               (необязательно; напр. claude-opus-4-8)
    CLAUDE_TIMEOUT=1800         (сек на один ход агента)
    WHISPER_BIN / WHISPER_MODEL / WHISPER_LANG / FFMPEG_BIN  (голос и видео)

Состояние (session_id, стоимость, offset, effort, side-чаты) переживает рестарт:
    ~/.config/tgbridge/state.json
"""
import os, sys, json, glob, time, shlex, signal, socket, subprocess, threading, collections
import urllib.request, urllib.parse, urllib.error
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))   # соседние tg*-модули находятся при любом запуске
from tgconfig import (STATE_FILE, log, C, TOKEN, ALLOWED_ID, WORKDIR, CLAUDE_BIN, PERM_MODE,
                      EFFORT_LEVELS, TIMEOUT, API, INCOMING, OUTGOING, TGSEND, LONG_LIMIT,
                      BASH_TIMEOUT, BASH_ALLOW, BASH_DENY, TG_FILE_LIMIT, full_sysprompt)
from tgformat import md_to_html, _chunks, context_prefix
from tgmedia import extract_audio, transcribe

# --- изменяемое в рантайме состояние (в tgconfig нельзя: from-import скопировал бы значение) ---
EFFORT   = C.get("EFFORT", "high")   # low|medium|high|xhigh|max; /effort меняет на лету
MODEL    = C.get("CLAUDE_MODEL", "")  # "" = модель по умолчанию самого CLI; /model меняет на лету
bash_cwd = WORKDIR                    # !cd меняет

# Короткие имена для /model. CLI понимает и алиас, и полный id, но свой список нужен, чтобы
# опечатка не улетела в claude и не убила ход невнятной сетевой ошибкой.
MODEL_ALIASES = {
    "opus":    "claude-opus-5",
    "opus5":   "claude-opus-5",
    "opus48":  "claude-opus-4-8",
    "sonnet":  "claude-sonnet-5",
    "fable":   "claude-fable-5",
    "haiku":   "claude-haiku-4-5",    # окно 200K против 1M у остальных
    "default": "",                    # снять переопределение, вернуться к модели CLI
}

# Живой статус долгого хода. Видно только в stream-json: обычный --output-format json
# отдаёт лишь финальный объект, и снаружи не понять, агент думает или ушёл в веб на 3 минуты
# (именно так и выглядело «бот молчит» на голосовом вопросе про население).
TOOL_LABELS = {
    "WebSearch": "🔍 ищу в вебе",     "WebFetch": "🌐 читаю страницу",
    "Bash":      "🛠 выполняю команды",
    "Read": "📂 читаю файлы",         "Glob": "📂 ищу файлы",  "Grep": "📂 ищу по файлам",
    "Edit": "✏️ правлю файлы",        "Write": "✏️ пишу файлы", "NotebookEdit": "✏️ правлю ноутбук",
    "Task": "🤖 запустил субагента",   "TodoWrite": "📝 планирую",
}
SLOW_TOOLS    = ("WebSearch", "WebFetch", "Task")   # сами по себе долгие -> говорим сразу, не ждём STATUS_AFTER
NET_HINTS     = ("curl ", "wget ", "http://", "https://")   # bash тоже ходит в сеть — и выглядеть должен так же
STATUS_AFTER  = 60    # сек до первого статуса: короткие ходы не трогаем
STATUS_EARLY  = 60    # сек: раньше этого молчим даже про долгий инструмент
STATUS_REPEAT = 90    # сек между повторами
STATUS_MAX    = 3     # максимум статусов за ход (анти-спам)

def _activity(name, inp):
    """(метка для человека, долгий ли). Агент часто лезет в интернет не WebSearch'ем,
    а `curl` в Bash — и тогда «выполняю команды» ничего не объясняет: надо «хожу в сеть»."""
    if name == "Bash":
        cmd = str((inp or {}).get("command") or "").lower()
        if any(h in cmd for h in NET_HINTS):
            return "🌐 хожу в сеть", True
    return TOOL_LABELS.get(name, "🛠 " + str(name)), name in SLOW_TOOLS

# ---------- state ----------
session_id  = None            # текущая claude-сессия основного диалога (для --resume)
last_data   = None            # последний json основного диалога (для /context)
total_cost  = 0.0             # ≈ по API-тарифу, за всё время; с подписки НЕ списывается
offset      = None            # getUpdates offset (персистится, переживает рестарт)
reset_gen   = 0               # счётчик /reset: гонка «reset во время хода»
main_lock   = threading.Lock()   # ходы главного диалога (сериализуются между собой)
side_lock   = threading.Lock()   # /btw, reply-ветки, loop — своя дорожка, параллельно главному
cost_lock   = threading.Lock()   # total_cost += из двух дорожек одновременно -> без гонки
current_procs = set()         # живые Popen агента (для /cancel; их может быть до двух)
proc_lock   = threading.Lock()
loop_stop   = None            # threading.Event текущего loop (None если нет)
loop_thread = None
btw_threads = collections.OrderedDict()   # message_id(int) -> side session_id(str)
BTW_CAP     = 200
state_lock  = threading.Lock()            # save_state зовут из разных потоков -> сериализуем запись
pending_reactions = []                    # реакции владельца, ждут следующего вопроса (персистятся)
REACT_CAP   = 20
bot_msgs    = collections.OrderedDict()   # message_id(int) -> сниппет ответа бота (чтобы знать, на ЧТО реакция)
BOTMSG_CAP  = 200
msgs_lock   = threading.Lock()            # bot_msgs мутируют разные потоки (send из двух дорожек)
bash_lock   = threading.Lock()            # !-команды строго по одной: общий bash_cwd
jobs_active = 0                           # ходы агента: в работе + в очереди (для /status)
jobs_lock   = threading.Lock()
ignored_seen = collections.OrderedDict()  # чужой id -> [кол-во, время последнего лога] (антифлуд-троттлинг)
IGNORED_CAP  = 500
alert_window = {}                         # чужой id -> кол-во с последней сводки владельцу
last_alert_ts = 0.0
ignored_lock  = threading.Lock()
IGNORE_LOG_EVERY = 60                     # тот же чужой id логируем не чаще раза в 60с
ALERT_COOLDOWN   = 300                    # сводку владельцу — не чаще раза в 5 мин (флуд не спамит уже тебя)

class Job:
    """Счётчик ходов агента. offset подтверждается при приёме (at-most-once) —
    перед перезапуском моста смотри /status: «задач у агента» должно быть 0."""
    def __enter__(self):
        global jobs_active
        with jobs_lock:
            jobs_active += 1
        return self
    def __exit__(self, *a):
        global jobs_active
        with jobs_lock:
            jobs_active -= 1

# ---------- персистентное состояние ----------
def save_state():
    with state_lock:                    # без лока конкурентные потоки бьют общий .tmp
        try:
            data = {"session_id": session_id, "total_cost": total_cost, "offset": offset,
                    "effort": EFFORT, "model": MODEL,
                    "btw": {str(k): v for k, v in btw_threads.items()},
                    "reactions": pending_reactions[:]}
            tmp = STATE_FILE + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(data, f)
            os.chmod(tmp, 0o600)            # как config.env рядом
            os.replace(tmp, STATE_FILE)     # атомарно
        except Exception as e:
            log("save_state err", repr(e))

def load_state():
    global session_id, total_cost, offset, EFFORT, MODEL
    try:
        if not os.path.isfile(STATE_FILE):
            return
        d = json.load(open(STATE_FILE, encoding="utf-8"))
        try:
            os.chmod(STATE_FILE, 0o600)     # старый state мог остаться с широкими правами
        except OSError:
            pass
        session_id = d.get("session_id")
        total_cost = float(d.get("total_cost", 0) or 0)
        offset     = d.get("offset")
        eff        = d.get("effort")
        if eff in EFFORT_LEVELS:
            EFFORT = eff
        mdl = d.get("model")
        if isinstance(mdl, str):            # "" — валидное значение (модель по умолчанию CLI)
            MODEL = mdl
        for k, v in (d.get("btw") or {}).items():
            try:
                btw_threads[int(k)] = v
            except Exception:
                pass
        pending_reactions[:] = [str(x) for x in (d.get("reactions") or [])][-REACT_CAP:]
        log(f"state loaded: session={str(session_id)[:8]} offset={offset} effort={EFFORT} model={MODEL or 'default'} btw={len(btw_threads)} react={len(pending_reactions)}")
    except Exception as e:
        log("load_state err", repr(e))

# ---------- telegram ----------
def tg(method, **params):
    """POST в Telegram. Возврат: dict (в т.ч. {"ok": false,...} на ошибку API) или None (сетевой сбой)."""
    data = urllib.parse.urlencode({k: v for k, v in params.items() if v is not None}).encode()
    try:
        with urllib.request.urlopen(f"{API}/{method}", data=data, timeout=70) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:                 # API ответил, но с ошибкой -> отдаём тело
        try:
            return json.loads(e.read())
        except Exception:
            log("TG HTTP", method, e.code)
            return None
    except Exception as e:                              # сеть недоступна / таймаут
        log("TG err", method, repr(e))
        return None

def _tg_send_retry(**params):
    """sendMessage с ретраями по сетевым сбоям (None). Возврат dict или None."""
    for delay in (0, 2, 5, 10):
        if delay:
            time.sleep(delay)
        r = tg("sendMessage", **params)
        if r is not None:                               # получили ответ (ok True или False) — сеть жива
            return r
    return None

def send(chat_id, text):
    """Отправить текст (markdown->HTML, с ретраями и plain-fallback). Возврат: список message_id."""
    text = text if (text and text.strip()) else "(пустой ответ)"
    ids = []
    for chunk in _chunks(text):
        r = _tg_send_retry(chat_id=chat_id, text=md_to_html(chunk),
                           parse_mode="HTML", disable_web_page_preview="true")
        if r is None:                                   # сеть легла — пробуем plain (тоже с ретраями)
            r = _tg_send_retry(chat_id=chat_id, text=chunk)
        elif not r.get("ok"):                           # HTML не распарсился -> plain, чтобы не потерять
            r = _tg_send_retry(chat_id=chat_id, text=chunk) or r
        if r and r.get("ok"):
            mid = r["result"]["message_id"]
            ids.append(mid)
            snip = " ".join(chunk.split())[:150]                 # для контекста реакций: на ЧТО поставили
            with msgs_lock:
                bot_msgs[mid] = snip + ("…" if len(chunk) > 150 else "")
                while len(bot_msgs) > BOTMSG_CAP:
                    bot_msgs.popitem(last=False)
        else:
            log("send failed", str((r or {}).get("description", "network"))[:120])
    return ids

def send_reply(chat_id, text):
    """Ответ агента: очень длинный -> файлом, иначе обычным сообщением. Возврат: список message_id."""
    text = text if (text and text.strip()) else "(пустой ответ)"
    if len(text) > LONG_LIMIT:
        # ns + id потока: main и side могут закончить в одну секунду -> имена не должны сталкиваться
        path = os.path.join(OUTGOING, f"answer_{time.time_ns()}_{threading.get_ident()}.md")
        try:
            with open(path, "w", encoding="utf-8") as f:
                f.write(text)
            ids = send(chat_id, "📄 Ответ длинный — присылаю файлом.\n\n" + " ".join(text[:280].split()) + "…")
            subprocess.run([TGSEND, path, "ответ Claude"], timeout=60, check=True)  # fail -> фолбэк текстом ниже
            return ids
        except Exception as e:
            log("send_reply file err", repr(e))
        finally:
            try: os.remove(path)            # файл — транспорт; сам текст и так остаётся в чате
            except OSError: pass
    return send(chat_id, text)

def typing(chat_id):
    tg("sendChatAction", chat_id=chat_id, action="typing")

class Typing:
    """Держит индикатор 'печатает…' пока агент работает."""
    def __init__(self, chat_id): self.chat_id = chat_id; self.ev = threading.Event()
    def __enter__(self):
        typing(self.chat_id)
        def loop():
            while not self.ev.wait(4): typing(self.chat_id)
        threading.Thread(target=loop, daemon=True).start(); return self
    def __exit__(self, *a): self.ev.set()

def download_file(file_id, name_hint=""):
    """Скачать присланный файл с Telegram на диск, вернуть путь или None."""
    r = tg("getFile", file_id=file_id)
    if not r or not r.get("ok"):
        return None
    fp = r["result"].get("file_path", "")
    if not fp:
        return None
    ext = os.path.splitext(fp)[1] or os.path.splitext(name_hint)[1] or ".bin"
    dest = os.path.join(INCOMING, f"{time.time_ns()}_{file_id[-8:]}{ext}")   # ns: без коллизий в одну секунду
    try:
        urllib.request.urlretrieve(f"https://api.telegram.org/file/bot{TOKEN}/{fp}", dest)
        return dest
    except Exception as e:
        log("download err", repr(e))
        return None

# ---------- claude ----------
def _kill(p):
    """SIGTERM группе процессов, через 5с — SIGKILL (claude плодит детей-bash)."""
    try:
        os.killpg(os.getpgid(p.pid), signal.SIGTERM)
    except Exception:
        try: p.terminate()
        except Exception: pass
    def hard():
        if p.poll() is None:
            try: os.killpg(os.getpgid(p.pid), signal.SIGKILL)
            except Exception:
                try: p.kill()
                except Exception: pass
    t = threading.Timer(5, hard); t.daemon = True; t.start()

def cancel_all():
    """Убить все текущие ходы агента (главный и side). Вернуть, сколько убили."""
    with proc_lock:
        procs = [p for p in current_procs if p.poll() is None]
    for p in procs:
        _kill(p)
    return len(procs)

def add_cost(data):
    """Прибавить стоимость хода. Зовётся из двух дорожек -> под cost_lock (иначе потерянные +=)."""
    global total_cost
    with cost_lock:
        total_cost += (data or {}).get("total_cost_usd", 0) or 0

def run_claude(prompt, resume=None, fork=False, effort=None, status_chat=None):
    """Запустить headless claude (промпт через stdin). Возврат (текст, data|None) — контракт
    тот же, что и раньше: финальное событие type=result содержит result/session_id/usage/
    modelUsage/total_cost_usd/is_error, то есть ровно то, что читают /context и add_cost.

    stream-json нужен ради живого статуса: события идут построчно, поэтому видно, каким
    инструментом агент занят прямо сейчас. status_chat != None -> в этот чат уйдёт статус
    долгого хода. Передавать только с пользовательских дорожек (не из loop и не из /compact).
    """
    cmd = [CLAUDE_BIN, "-p", "--output-format", "stream-json", "--verbose",
           "--permission-mode", PERM_MODE, "--effort", effort or EFFORT,
           "--append-system-prompt", full_sysprompt()]
    if MODEL:            cmd += ["--model", MODEL]
    if resume:           cmd += ["--resume", resume]
    if fork and resume:  cmd += ["--fork-session"]      # side-чат: новый session_id, основной не трогаем
    try:
        p = subprocess.Popen(cmd, cwd=WORKDIR, stdin=subprocess.PIPE,
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                             text=True, start_new_session=True)   # своя группа -> /cancel убьёт и детей
    except Exception as e:
        return f"⚠️ Не смог запустить агента: {e}", None
    with proc_lock:
        current_procs.add(p)

    box  = {"data": None, "act": None, "slow": False, "raw": [], "err": []}
    done = threading.Event()
    t0   = time.time()

    def pump_out():
        try:
            for line in p.stdout:                       # построчно, ПОКА агент работает
                line = line.strip()
                if not line:
                    continue
                try:
                    d = json.loads(line)
                except Exception:
                    box["raw"].append(line[:500]); continue
                if d.get("type") == "result":
                    box["data"] = d
                elif d.get("type") == "assistant":
                    for b in ((d.get("message") or {}).get("content") or []):
                        if b.get("type") == "tool_use":
                            box["act"], box["slow"] = _activity(b.get("name"), b.get("input"))
        except Exception as e:
            log("stream read err", repr(e))

    def pump_err():
        try:
            for line in p.stderr:      # вычерпывать ОБЯЗАТЕЛЬНО: полная труба stderr вешает агента
                box["err"].append(line.rstrip()[:500])
        except Exception:
            pass

    def reporter():
        sent, last_at, last_act = 0, 0.0, None
        while not done.wait(1):                         # done ставится в finally -> статус не переживёт ход
            if sent >= STATUS_MAX:
                return
            el, act = time.time() - t0, box["act"]
            # по расписанию — или сразу, как только агент ушёл в заведомо долгое (веб, субагент)
            due   = (el >= STATUS_AFTER) if sent == 0 else (el - last_at >= STATUS_REPEAT)
            early = (box["slow"] and act != last_act
                     and el >= STATUS_EARLY and (sent == 0 or el - last_at >= STATUS_EARLY))
            if not (due or early):
                continue
            send(status_chat, f"⏳ {act or '🤔 думаю'}… (уже {int(el)} с)")
            sent, last_at, last_act = sent + 1, el, act

    t_out = threading.Thread(target=pump_out, daemon=True)
    t_err = threading.Thread(target=pump_err, daemon=True)
    t_out.start(); t_err.start()
    if status_chat:
        threading.Thread(target=reporter, daemon=True).start()
    try:
        try:
            p.stdin.write(prompt); p.stdin.close()
        except Exception as e:                          # агент мог упасть раньше — BrokenPipe не роняет ход
            log("stdin err", repr(e))
        try:
            p.wait(timeout=TIMEOUT)
        except subprocess.TimeoutExpired:
            _kill(p)
            try: p.wait(timeout=8)
            except Exception: pass
            return f"⏱️ Агент не уложился в {TIMEOUT}s.", None
        t_out.join(timeout=10)                          # дочитать хвост stdout после выхода процесса
    except Exception as e:
        return f"⚠️ Ошибка агента: {e}", None
    finally:
        done.set()
        with proc_lock:
            current_procs.discard(p)
        for s in (p.stdout, p.stderr):
            try: s.close()
            except Exception: pass

    if p.returncode is not None and p.returncode < 0:   # убит сигналом (/cancel или таймаут-kill)
        return "⏹️ (прервано)", None
    data = box["data"]
    if not data:                                        # нет result-события: отдать что было, как и раньше
        tail = "\n".join(box["raw"][-3:]) or "\n".join(box["err"][-3:])
        return (tail.strip() or "(пустой ответ)")[:3900], None
    if data.get("is_error"):
        return "⚠️ Ошибка агента: " + str(data.get("result", ""))[:1500], data
    return data.get("result", ""), data

PROJECTS_DIR = os.path.expanduser("~/.claude/projects")

def _usage_sum(u):
    u = u or {}
    return u.get("input_tokens", 0) + u.get("cache_read_input_tokens", 0) + u.get("cache_creation_input_tokens", 0)

def ctx_from_transcript(sid):
    """Точный размер контекста: usage последнего API-запроса сессии, из её транскрипта.

    Нужно потому, что usage в result-событии — СУММА по всем запросам хода: ход с одним
    вызовом инструмента = 2 запроса, и контекст в отчёте удваивался (853k вместо 427k).
    """
    if not sid:
        return 0
    hits = glob.glob(os.path.join(PROJECTS_DIR, "*", f"{sid}.jsonl"))
    if not hits:
        return 0
    path = max(hits, key=os.path.getmtime)
    try:
        with open(path, "rb") as f:                     # транскрипт бывает на сотню МБ — читаем только хвост
            f.seek(0, os.SEEK_END)
            f.seek(max(0, f.tell() - 400_000))
            tail = f.read().decode("utf-8", "replace")
    except Exception as e:
        log("ctx transcript err", repr(e))
        return 0
    for line in reversed(tail.split("\n")):
        if '"assistant"' not in line:
            continue
        try: d = json.loads(line)
        except Exception: continue                      # первая строка хвоста почти всегда обрезана
        if d.get("type") != "assistant":
            continue
        n = _usage_sum((d.get("message") or {}).get("usage"))
        if n:                                           # первый ход после /compact пишет нули
            return n
    return 0

def ctx_tokens(d):
    """Сколько токенов контекста занимает диалог сейчас."""
    n = ctx_from_transcript((d or {}).get("session_id"))
    return n or _usage_sum((d or {}).get("usage"))      # fallback: завышает при tool-use, но лучше нуля

def fmt_context(d):
    ctx = ctx_tokens(d)
    win = max((m.get("contextWindow", 0) for m in d.get("modelUsage", {}).values()), default=200000)
    pct = 100 * ctx / win if win else 0
    return (f"🧮 Контекст (последний ход): ~{ctx:,} / {win:,} токенов ({pct:.1f}%)\n"
            f"≈ по API-тарифу: ${total_cost:.4f} (справочно; с подписки НЕ списывается)")

def status_text():
    running = (loop_thread is not None and loop_thread.is_alive()
               and loop_stop is not None and not loop_stop.is_set())
    return ("🟢 tgbridge живёт\n"
            f"диалог: {'активен ' + session_id[:8] + '…' if session_id else 'новый (пусто)'}\n"
            f"стоимость: ${total_cost:.4f} (за всё время, ≈ API-тариф; с подписки НЕ списывается)\n"
            f"loop: {'работает' if running else 'нет'}\n"
            f"агент занят: главный={'да' if main_lock.locked() else 'нет'}, side={'да' if side_lock.locked() else 'нет'}\n"
            f"задач у агента (работа+очередь): {jobs_active}  ← перед рестартом моста должно быть 0\n"
            f"side-чатов в памяти: {len(btw_threads)}\n"
            f"effort: {EFFORT}\n"
            f"модель: {MODEL or 'по умолчанию (CLI)'}\n"
            f"workdir: {WORKDIR}")

HELP = (
    "🤖 tgbridge — Claude Code по телеге\n\n"
    "Просто пиши сообщение — оно уйдёт агенту (со всеми инструментами: bash, ssh, файлы).\n\n"
    "Команды:\n"
    "/reset — начать новый диалог\n"
    "/status — статус моста\n"
    "/context — заполнение контекста + стоимость\n"
    "/cancel — прервать текущий ход агента (и loop)\n"
    "/effort low|medium|high|xhigh|max — скорость/качество на лету\n"
    "/model opus|opus48|sonnet|fable|haiku|default — сменить модель (контекст диалога сохраняется)\n"
    "/btw <вопрос> — side-чат: видит контекст диалога, но НЕ влияет на него.\n"
    "   Продолжить side-чат — ответь (reply) на 💬-сообщение бота.\n"
    "   Вернулся в главный (обычное сообщение) — все боковые закрываются.\n"
    "   Поздний 💬-ответ (пришёл уже после закрытия) — тоже можно продолжить reply'ем.\n"
    "/loop <сек> <промпт> — повторять промпт каждые N сек\n"
    "/loop stop  (или /stop) — остановить loop\n"
    "/compact — сжать контекст диалога (покажу размер до → после)\n"
    "Другие claude-слэши (/cost, кастомные) — пробрасываются агенту\n"
    "!<команда> — быстрый bash из белого списка (ls, cat, grep, git, cd, chmod…), без rm/mv\n\n"
    "🎙 Голосовое — распознаю (whisper) и выполню как команду.\n"
    "🎥 Видео — вытащу звук и распознаю (сам видеоряд не смотрю).\n"
    "📎 Картинку или файл — агент их посмотрит (через Read); альбомы тоже.\n"
    "✏️ Правка недавнего (≤5 мин) сообщения = перезапуск с исправленным текстом.\n"
    "👍 Реакция на мой ответ — учту её как обратную связь при твоём СЛЕДУЮЩЕМ вопросе (отдельный запрос не трачу).\n"
    "Попроси «пришли мне <файл>» — агент отправит файл в чат (длинные ответы тоже приходят файлом).")

# Команды для меню Telegram (всплывают по "/"; регистрируются в setMyCommands при старте).
BOT_COMMANDS = [
    {"command": "reset",   "description": "начать новый диалог"},
    {"command": "status",  "description": "статус: сессия, стоимость, боковые чаты"},
    {"command": "context", "description": "заполнение контекста + стоимость"},
    {"command": "cancel",  "description": "прервать текущий ход агента"},
    {"command": "effort",  "description": "reasoning: low | medium | high | xhigh | max"},
    {"command": "model",   "description": "модель: opus | opus48 | sonnet | fable | haiku"},
    {"command": "btw",     "description": "боковой вопрос, не трогая главный диалог"},
    {"command": "pay",     "description": "платежи: оплатил X / покажи таблицу / добавь платёж"},
    {"command": "loop",    "description": "повторять промпт каждые N сек (/loop stop — стоп)"},
    {"command": "compact", "description": "сжать контекст (покажу до → после)"},
    {"command": "help",    "description": "что умею и все команды"},
]

# ---------- loop ----------
def start_loop(chat_id, interval, prompt):
    global loop_thread, loop_stop
    if loop_stop:
        loop_stop.set()                                 # сигнал прошлому loop; выйдет после текущего тика
    stop = threading.Event()                            # свой Event на этот запуск -> нет гонки двойного loop
    loop_stop = stop
    def runner():
        send(chat_id, f"🔁 Loop запущен: каждые {interval}s.\nОстановить: /loop stop")
        while not stop.is_set():
            with Job(), side_lock:                      # loop на side-дорожке: не морозит главный диалог
                if stop.is_set(): break
                res, data = run_claude(prompt, resume=None)   # каждый тик — свежая сессия
            add_cost(data)
            save_state()
            if stop.is_set(): break
            send(chat_id, "🔁 " + res)
            stop.wait(interval)
        send(chat_id, "⏹️ Loop остановлен.")
    loop_thread = threading.Thread(target=runner, daemon=True)
    loop_thread.start()

def handle_loop(chat_id, text):
    parts = text.split(maxsplit=2)
    if len(parts) >= 2 and parts[1].lower() in ("stop", "off"):
        if loop_stop: loop_stop.set()
        return
    if len(parts) < 3:
        send(chat_id, "Использование: /loop <секунды> <промпт>\nОстановить: /loop stop"); return
    try:
        interval = max(30, int(parts[1]))
    except ValueError:
        send(chat_id, "Первый аргумент — секунды (число ≥ 30)."); return
    start_loop(chat_id, interval, parts[2])

# ---------- ! bash (ограниченный белый список, как в jupyter) ----------
def handle_bang(chat_id, cmd):
    with bash_lock:                         # !-команды из потоков делят bash_cwd -> строго по одной
        _handle_bang(chat_id, cmd)

def _handle_bang(chat_id, cmd):
    global bash_cwd
    if not cmd:
        send(chat_id, "Использование: !<команда>, напр. !ls -la или !cd ~/dev\nРазрешено: " + " ".join(sorted(BASH_ALLOW))); return
    try:
        toks = shlex.split(cmd)
    except ValueError as e:
        send(chat_id, f"Не разобрал команду: {e}"); return
    if not toks:
        return
    first = os.path.basename(toks[0])
    if first == "cd":                                       # встроенная: меняем рабочую папку !-сессии
        tgt = os.path.expanduser(os.path.expandvars(toks[1])) if len(toks) > 1 else os.path.expanduser("~")
        if not os.path.isabs(tgt):
            tgt = os.path.join(bash_cwd, tgt)
        tgt = os.path.normpath(tgt)
        if os.path.isdir(tgt):
            bash_cwd = tgt; send(chat_id, f"📂 {bash_cwd}")
        else:
            send(chat_id, f"⚠️ нет папки: {tgt}")
        return
    if first in BASH_DENY or first not in BASH_ALLOW:
        send(chat_id, f"⛔ «{first}» не в белом списке для !. Сложное — попроси агента обычным сообщением.\n"
                      "Разрешено: " + " ".join(sorted(BASH_ALLOW))); return
    args = [os.path.expanduser(os.path.expandvars(a)) for a in toks]     # раскрыть ~/$VAR, без shell
    env = dict(os.environ, PATH=os.path.dirname(TGSEND) + ":" + os.environ.get("PATH", ""))
    try:
        p = subprocess.run(args, cwd=bash_cwd, capture_output=True, text=True, timeout=BASH_TIMEOUT, env=env)
    except subprocess.TimeoutExpired:
        send(chat_id, f"⏱️ !{first}: таймаут {BASH_TIMEOUT}s"); return
    except FileNotFoundError:
        send(chat_id, f"⚠️ не найдено: {first}"); return
    except Exception as e:
        send(chat_id, f"⚠️ ошибка: {e}"); return
    out = p.stdout + (("\n[stderr]\n" + p.stderr) if p.stderr.strip() else "")
    out = out.rstrip() or "(пусто)"
    if len(out) > 3500:
        out = out[:3500] + "\n…(обрезано)"
    tail = "" if p.returncode == 0 else f"\n(код возврата {p.returncode})"
    send(chat_id, f"```\n{out}\n```{tail}")

# ---------- side-чаты (/btw + reply-ветки) ----------
def register_btw(ids, sid):
    """Привязать message_id ответов бота к side-сессии, чтобы reply на них продолжал ветку."""
    if not sid or not ids:
        return
    with state_lock:                    # мутации btw_threads под state_lock: save_state итерирует его
        for mid in ids:
            btw_threads[mid] = sid
        while len(btw_threads) > BTW_CAP:
            btw_threads.popitem(last=False)
    save_state()                        # лок отпущен: save_state берёт state_lock сам (не реентрантный)

# ---------- реакции (👍 и пр.): не отдельный ход, а контекст к следующему вопросу ----------
def _react_emojis(lst):
    out = []
    for r in (lst or []):
        t = r.get("type")
        if t == "emoji" and r.get("emoji"):   out.append(r["emoji"])
        elif t == "custom_emoji":             out.append("✨")
        elif t == "paid":                     out.append("⭐")
    return out

def record_reaction(mr):
    """Запомнить реакцию владельца. НЕ дёргает агента — уедет контекстом к следующему вопросу."""
    added = [e for e in _react_emojis(mr.get("new_reaction")) if e not in _react_emojis(mr.get("old_reaction"))]
    if not added:                             # реакцию сняли / нечего добавлять
        return
    mid = mr.get("message_id")
    with msgs_lock:
        snip = bot_msgs.get(mid)
    if snip:
        desc = f"[Владелец поставил реакцию {' '.join(added)} на твой ответ «{snip}» — учти как обратную связь.]"
    else:
        desc = f"[Владелец поставил реакцию {' '.join(added)} на твоё сообщение — учти как обратную связь.]"
    with state_lock:
        pending_reactions.append(desc)
        del pending_reactions[:-REACT_CAP]    # держим последние REACT_CAP
    save_state()
    log(f"reaction {added} on msg {mid} -> буфер ({len(pending_reactions)})")

def take_reactions():
    """Забрать накопленные реакции как префикс к промпту и очистить (ровно один ход их получит)."""
    with state_lock:
        if not pending_reactions:
            return ""
        lines = pending_reactions[:]; pending_reactions.clear()
    save_state()
    return "\n".join(lines) + "\n"

def handle_side(chat_id, text, sid):
    """Продолжение side-ветки: reply на 💬-сообщение бота."""
    text = take_reactions() + text              # реакции уедут этим ходом
    with Job(), Typing(chat_id):
        with side_lock:
            res, data = run_claude(text, resume=sid, status_chat=chat_id)  # без fork: продолжаем эту ветку
    add_cost(data)
    save_state()
    new_sid = (data or {}).get("session_id") or sid
    ids = send_reply(chat_id, "💬 " + res)
    register_btw(ids, new_sid)

def safe_handle_side(chat_id, text, sid):
    try:
        handle_side(chat_id, text, sid)
    except Exception as e:
        log("side err", repr(e)); send(chat_id, f"⚠️ Ошибка side-чата: {e}")

# ---------- dispatch ----------
def handle(chat_id, text):
    global session_id, last_data, reset_gen, EFFORT, MODEL
    t = text.strip()
    if t.lower() == "/pay" or t.lower().startswith("/pay "):   # маркер «это про платежи» -> обычный ход агента, не слэш Claude
        rest = t[4:].strip() or "покажи таблицу платежей"
        t = text = "[ПЛАТЕЖИ] " + rest
    low = t.lower()

    if low in ("/start", "/help"):
        send(chat_id, HELP); return
    if low == "/reset":
        reset_gen += 1; session_id = None; last_data = None; save_state()
        send(chat_id, "🔄 Начал новый диалог."); return
    if low == "/status":
        send(chat_id, status_text()); return
    if low == "/context":
        send(chat_id, fmt_context(last_data) if last_data else "Пока нет данных — напиши что-нибудь агенту."); return
    if low in ("/stop", "/cancel"):
        if loop_stop: loop_stop.set()
        if low == "/cancel":
            n = cancel_all()
            if n:
                send(chat_id, f"⏹️ Прервал текущие ходы ({n})."); return
        send(chat_id, "⏹️ Остановил loop (если был)."); return
    if low.startswith("/effort"):
        parts = t.split()
        if len(parts) < 2:
            send(chat_id, f"Текущий effort: {EFFORT}\nИспользование: /effort low|medium|high|xhigh|max"); return
        lvl = parts[1].lower()
        if lvl not in EFFORT_LEVELS:
            send(chat_id, "Уровни: low | medium | high | xhigh | max"); return
        old = EFFORT; EFFORT = lvl; save_state()
        send(chat_id, f"⚙️ effort: {old} → {EFFORT}"); return
    if low.startswith("/model"):
        parts = t.split()
        if len(parts) < 2:
            send(chat_id, f"Текущая модель: {MODEL or 'по умолчанию (CLI)'}\n"
                          f"Использование: /model {' | '.join(MODEL_ALIASES)}\n"
                          "или полный id: /model claude-opus-5"); return
        arg = parts[1].lower()
        if arg in MODEL_ALIASES:
            new = MODEL_ALIASES[arg]
        elif arg.startswith("claude-"):
            new = arg                       # сырой id: новые модели без правки кода
        else:
            send(chat_id, f"Не знаю модель «{parts[1]}».\n"
                          f"Алиасы: {' | '.join(MODEL_ALIASES)}\n"
                          "или полный id, начинающийся с claude-"); return
        old = MODEL; MODEL = new; save_state()
        msg = (f"🧠 модель: {old or 'по умолчанию'} → {MODEL or 'по умолчанию'}\n"
               "Применится со следующего хода. Контекст диалога сохраняется, но кэш промпта "
               "сбросится — первый ход будет чуть дороже и медленнее.")
        if "haiku" in MODEL:
            msg += "\n⚠️ У haiku окно 200K против 1M у остальных — на разросшемся диалоге может не влезть."
        send(chat_id, msg); return
    if low.startswith("/loop"):
        handle_loop(chat_id, t); return
    if low.startswith("/btw"):
        q = t[4:].strip()
        if not q:
            send(chat_id, "Использование: /btw <вопрос> — side-чат: видит контекст диалога, но не меняет его.\n"
                          "Продолжить — ответь (reply) на 💬-сообщение."); return
        q = take_reactions() + q                 # реакции уедут этим side-ходом
        with Job(), Typing(chat_id):
            with side_lock:
                res, data = run_claude(q, resume=session_id, fork=bool(session_id), status_chat=chat_id)
        add_cost(data)
        save_state()
        sid = (data or {}).get("session_id")
        hint = "\n\n↩️ Ответь (reply) на это сообщение, чтобы продолжить side-чат." if sid else ""
        ids = send_reply(chat_id, "💬 (side) " + res + hint)
        register_btw(ids, sid)
        return
    if low.startswith("/compact"):
        if not session_id:
            send(chat_id, "Диалог пуст — сжимать нечего. Напиши что-нибудь агенту."); return
        before = ctx_tokens(last_data)                  # контекст последнего хода ДО сжатия
        send(chat_id, "🗜 Сжимаю контекст…")
        pres, pdata = "", None
        with Job(), Typing(chat_id):
            with main_lock:
                gen = reset_gen
                res, data = run_claude(t, resume=session_id)
                add_cost(data)
                if gen == reset_gen and data and data.get("session_id"):
                    session_id = data["session_id"]
                if data is None:                        # ошибка или /cancel
                    save_state(); send(chat_id, res); return
                if res.strip() and "not enough" in res.lower():   # компактить нечего
                    save_state(); send(chat_id, "🤏 " + res.strip() + "\nКонтекст и так небольшой — сжимать нечего."); return
                # usage compact-хода не отражает новый размер -> дешёвый пробный ход для честного замера
                pres, pdata = run_claude("Ответь ровно: ок", resume=session_id, effort="low")
                add_cost(pdata)
                if gen == reset_gen and pdata and pdata.get("session_id"):
                    session_id = pdata["session_id"]
                    last_data  = pdata
        save_state()
        if pdata:
            after = ctx_tokens(pdata)
            head = (f"✅ Контекст сжат: ~{before:,} → ~{after:,} токенов\n" if before else "✅ Контекст сжат.\n")
            send(chat_id, head + fmt_context(last_data))
        else:
            send(chat_id, "✅ Сжал, но замерить новый контекст не смог: " + pres)
        return

    # обычное сообщение ИЛИ проброс claude-слэша (/cost, кастомные слэши)
    if btw_threads and not t.startswith("/"):   # вернулся в главный (слэш-проброс — не «возврат») -> закрыть боковые
        with state_lock:                        # мутации btw_threads под state_lock: save_state итерирует его
            n = len(btw_threads); btw_threads.clear()
        save_state()
        log(f"side закрыты ({n}): владелец написал в главный")
        send(chat_id, f"🗂 Закрыл боковые чаты ({n}).")
    t = take_reactions() + t                     # реакции владельца (если были) уедут этим ходом
    with Job(), Typing(chat_id):
        with main_lock:
            gen = reset_gen
            res, data = run_claude(t, resume=session_id, status_chat=chat_id)
            if gen == reset_gen and data and data.get("session_id"):   # /reset во время хода -> не воскрешаем
                session_id = data["session_id"]
                last_data  = data
    add_cost(data)
    save_state()
    send_reply(chat_id, res)

def safe_handle(chat_id, text):
    try:
        handle(chat_id, text)
    except Exception as e:
        log("handle err", repr(e)); send(chat_id, f"⚠️ Внутренняя ошибка: {e}")

# ---------- альбомы (media groups: фото и документы) ----------
mg_lock = threading.Lock()
media_groups = {}   # media_group_id -> {items: [(kind,file_id,name)], caption, chat_id, timer}

def flush_group(mgid):
    with mg_lock:
        grp = media_groups.pop(mgid, None)
    if not grp:
        return
    items = []
    for kind, fid, name in grp["items"]:
        p = download_file(fid, name or ("img.jpg" if kind == "photo" else "file.bin"))
        if p:
            items.append((kind, p, name))
    if not items:
        send(grp["chat_id"], "⚠️ Не смог скачать файлы альбома."); return
    cap = grp["caption"].strip()
    listing = "; ".join(p + (f" (имя: {n})" if n else "") for _, p, n in items)
    allphoto = all(k == "photo" for k, _, _ in items)
    what = "изображений (альбом)" if allphoto else "файлов (альбом)"
    default = ("Посмотри ВСЕ изображения (через Read) и ответь по ним." if allphoto
               else "Посмотри/обработай все файлы и ответь.")
    prompt = f"[Пользователь прислал {len(items)} {what}, сохранены: {listing}]\n" + (cap or default)
    log(f"album {mgid}: {len(items)} шт (photo={allphoto})")
    safe_handle(grp["chat_id"], prompt)

def buffer_group(chat_id, mgid, kind, file_id, name, caption):
    with mg_lock:
        grp = media_groups.setdefault(mgid, {"items": [], "caption": "", "chat_id": chat_id, "timer": None})
        grp["items"].append((kind, file_id, name))
        if caption:
            grp["caption"] = caption
        if grp["timer"]:
            grp["timer"].cancel()
        grp["timer"] = threading.Timer(2.0, flush_group, args=[mgid])
        grp["timer"].daemon = True
        grp["timer"].start()

# ---------- уборка ----------
def cleanup_old_files():
    """Удалить из incoming/ и outgoing/ файлы старше 30 дней (медиа и длинные ответы — чувствительные)."""
    cutoff = time.time() - 30 * 86400
    for d in (INCOMING, OUTGOING):
        try:
            for name in os.listdir(d):
                fp = os.path.join(d, name)
                try:
                    if os.path.isfile(fp) and os.path.getmtime(fp) < cutoff:
                        os.remove(fp)
                except Exception:
                    pass
        except Exception as e:
            log("cleanup err", repr(e))

# ---------- main loop ----------
def note_ignored(frm, chat_id, text, now=None):
    """Чужое сообщение: троттлим лог, копим для сводки. Возврат: текст алерта владельцу или None.
    Логируем первое сообщение от нового id, дальше не чаще IGNORE_LOG_EVERY (со счётчиком).
    Сводку владельцу отдаём не чаще ALERT_COOLDOWN, чтобы флуд не превратился в спам ТЕБЕ."""
    global last_alert_ts
    now = now if now is not None else time.time()
    with ignored_lock:
        rec = ignored_seen.get(frm)
        first = rec is None
        if first:
            rec = [0, 0.0]; ignored_seen[frm] = rec
            while len(ignored_seen) > IGNORED_CAP:
                ignored_seen.popitem(last=False)
        rec[0] += 1
        count = rec[0]
        do_log = first or (now - rec[1] >= IGNORE_LOG_EVERY)
        if do_log:
            rec[1] = now
        alert_window[frm] = alert_window.get(frm, 0) + 1
        alert = None
        if now - last_alert_ts >= ALERT_COOLDOWN:       # пора предупредить владельца (и есть о чём — нас сюда позвали)
            last_alert_ts = now
            n_ids, n_msg = len(alert_window), sum(alert_window.values())
            ids_prev = ", ".join(str(k) for k in list(alert_window)[:5])
            alert_window.clear()
            alert = (f"👀 Кто-то стучится в бота: {n_ids} неизвестных id, {n_msg} сообщ. за ~{ALERT_COOLDOWN//60} мин.\n"
                     f"id: {ids_prev}{' …' if n_ids > 5 else ''}\n"
                     f"Все игнорируются — 0 токенов, ничего не выполнено. Если спамят и достаёт — пересоздай токен через BotFather.")
    if do_log:
        log(f"IGNORED from id={frm} chat={chat_id} (x{count}): {text[:60]!r}")
    return alert

def process_update(upd):
    """Один update: гейты и роутинг. Исключение отсюда не должно валить поллер (ловится в main)."""
    mr = upd.get("message_reaction")                     # реакция (👍 и пр.) -> буфер, БЕЗ вызова агента
    if mr is not None:
        u = mr.get("user") or {}
        if u.get("id") == ALLOWED_ID and (mr.get("chat") or {}).get("id") == ALLOWED_ID:
            record_reaction(mr)
        else:
            log(f"IGNORED reaction from id={u.get('id')}")
        return
    is_edit = upd.get("edited_message") is not None
    msg = upd.get("message") or upd.get("edited_message")
    if not msg:
        return
    frm = (msg.get("from") or {}).get("id")
    chat_id = msg["chat"]["id"]
    text = msg.get("text", "")
    if frm != ALLOWED_ID or chat_id != ALLOWED_ID:      # только владелец и только его личный чат
        alert = note_ignored(frm, chat_id, text)        # троттлит лог, копит сводку
        if alert:
            threading.Thread(target=send, args=(ALLOWED_ID, alert), daemon=True).start()  # алерт не блокирует поллер
        return
    if is_edit and (msg.get("edit_date", 0) - msg.get("date", 0)) > 300:
        log(f"IGNORED stale edit from {frm}: {text[:60]!r}")
        return
    if text.startswith("!"):                             # быстрый bash из белого списка (не через агента)
        threading.Thread(target=handle_bang, args=(chat_id, text[1:].strip()), daemon=True).start()
        return
    rt = msg.get("reply_to_message") or {}               # reply на 💬-сообщение -> продолжение side-ветки
    side_sid = btw_threads.get(rt.get("message_id")) if rt else None
    if side_sid and text and not text.startswith("/"):
        threading.Thread(target=safe_handle_side, args=(chat_id, text, side_sid), daemon=True).start()
        return
    mgid = msg.get("media_group_id")
    if mgid and (msg.get("photo") or msg.get("document")):   # альбом -> буферизуем, отдадим одним ходом
        if msg.get("photo"):
            buffer_group(chat_id, mgid, "photo", msg["photo"][-1]["file_id"], "", (msg.get("caption") or "").strip())
        else:
            doc = msg["document"]
            buffer_group(chat_id, mgid, "document", doc["file_id"], doc.get("file_name", ""), (msg.get("caption") or "").strip())
        return
    prompt = text
    if not prompt:                                       # не текст -> картинка/файл/голос/видео?
        cap = (msg.get("caption") or "").strip()
        if msg.get("photo"):
            path = download_file(msg["photo"][-1]["file_id"], "img.jpg")
            if not path:
                send(chat_id, "⚠️ Не смог скачать картинку."); return
            prompt = (f"[Пользователь прислал изображение, сохранено: {path}]\n"
                      + (cap or "Посмотри изображение (через Read) и ответь по нему."))
        elif msg.get("document"):
            doc = msg["document"]
            path = download_file(doc["file_id"], doc.get("file_name", ""))
            if not path:
                send(chat_id, "⚠️ Не смог скачать файл."); return
            prompt = (f"[Пользователь прислал файл, сохранён: {path} (имя: {doc.get('file_name','?')})]\n"
                      + (cap or "Посмотри/обработай этот файл и ответь."))
        elif msg.get("voice") or msg.get("audio"):       # голосовые и аудиофайлы (кружки -> ниже, как видео)
            v = msg.get("voice") or msg.get("audio")
            path = download_file(v["file_id"], "voice.ogg")
            if not path:
                send(chat_id, "⚠️ Не смог скачать голосовое."); return
            with Typing(chat_id):
                heard = transcribe(path)
            if not heard:
                send(chat_id, "⚠️ Не распознал голос (whisper). Проверь WHISPER_BIN в конфиге."); return
            send(chat_id, "🎙 услышал: «" + heard + "»")
            prompt = (cap + "\n" if cap else "") + heard
        elif msg.get("video") or msg.get("video_note"):  # видео И кружки: распознаём ТОЛЬКО звук
            vid = msg.get("video") or msg.get("video_note")
            is_note = msg.get("video_note") is not None
            if vid.get("file_size", 0) > TG_FILE_LIMIT:
                send(chat_id, "⚠️ Ролик больше 20 МБ — Bot API такое не отдаёт. Пришли покороче или сожми."); return
            path = download_file(vid["file_id"], "video.mp4")
            if not path:
                send(chat_id, "⚠️ Не смог скачать ролик."); return
            audio = extract_audio(path)
            if audio:
                try: os.remove(path)                     # тяжёлый исходник больше не нужен
                except Exception as e: log("rm video err", repr(e))
                tpath = audio
            else:
                tpath = path                             # ffmpeg не смог — whisper декодирует сам
            with Typing(chat_id):
                heard = transcribe(tpath)
            if tpath == path:
                try: os.remove(path)
                except Exception as e: log("rm video err", repr(e))
            if not heard:
                send(chat_id, "⚠️ Не расслышал речи (или там нет аудио)."); return
            send(chat_id, "🎙 услышал: «" + heard + "»")
            kind = "кружок (video_note)" if is_note else "видео"
            prompt = (f"[Владелец прислал {kind}; видеоряд НЕ анализируется — распознана только аудиодорожка. "
                      "Транскрипт ниже.]\n" + (cap + "\n" if cap else "") + heard)
        else:
            send(chat_id, "Умею текст, голос, видео (только звук), картинки и файлы (и могу присылать файлы по запросу)."); return
    if is_edit:
        prompt = "[правка сообщения — актуальная версия:]\n" + prompt
    prompt = context_prefix(msg) + prompt                # reply/forward-контекст
    log(f"msg from {frm}{' [edit]' if is_edit else ''}: {prompt[:80]!r}")
    threading.Thread(target=safe_handle, args=(chat_id, prompt), daemon=True).start()

def main():
    global offset
    load_state()
    socket.setdefaulttimeout(120)          # страховка: urlretrieve и т.п. не висят вечно
    me = tg("getMe")
    who = me["result"]["username"] if me and me.get("ok") else "??? (нет сети или неверный токен — жду)"
    log(f"tgbridge up | bot=@{who} | allowed_id={ALLOWED_ID} | workdir={WORKDIR} | claude={CLAUDE_BIN} | effort={EFFORT}")
    tg("deleteWebhook")                     # на случай ранее выставленного вебхука
    tg("setMyCommands", commands=json.dumps(BOT_COMMANDS, ensure_ascii=False))  # меню по "/"
    last_cleanup = 0
    while True:
        if time.time() - last_cleanup > 86400:
            last_cleanup = time.time(); cleanup_old_files()
        resp = tg("getUpdates", offset=offset, timeout=50,
                  allowed_updates='["message","edited_message","message_reaction"]')
        if not resp or not resp.get("ok"):
            time.sleep(3); continue
        for upd in resp["result"]:
            offset = upd["update_id"] + 1
            try:
                process_update(upd)                      # кривой update не должен валить поллер
            except Exception as e:
                log("update err", repr(e))
        if resp["result"]:
            # at-most-once: offset подтверждается при ПРИЁМЕ батча, не по завершении ходов.
            # Рестарт при живых/ожидающих задачах теряет их — смотри «задач у агента» в /status.
            save_state()

if __name__ == "__main__":
    main()
