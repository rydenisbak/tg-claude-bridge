#!/usr/bin/env python3
"""
tgbridge — Telegram <-> Claude Code (headless) bridge.

Ты пишешь боту -> проверка твоего TG id -> headless `claude -p` (stateful) -> ответ назад.
Отвечает ТОЛЬКО на сообщения от TG_ALLOWED_ID. Только stdlib, без pip-зависимостей.

Конфиг: ~/.config/tgbridge/config.env  (chmod 600)
    TG_BOT_TOKEN=...            (обязательно)
    TG_ALLOWED_ID=123456789     (обязательно; твой Telegram user id)
    WORKDIR=~/dev               (необязательно; рабочая папка агента, по умолч. HOME)
    CLAUDE_BIN=~/.local/bin/claude
    PERMISSION_MODE=bypassPermissions
    EFFORT=high                 (уровень reasoning: low|medium|high|xhigh|max)
    CLAUDE_MODEL=               (необязательно; напр. claude-opus-4-8)
    CLAUDE_TIMEOUT=1800         (сек на один ход агента)
"""
import os, sys, json, re, time, shlex, subprocess, threading, urllib.request, urllib.parse, urllib.error
import html as _html

CONFIG = os.path.expanduser("~/.config/tgbridge/config.env")

def load_config(path):
    cfg = {}
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            cfg[k.strip()] = v.strip().strip('"').strip("'")
    return cfg

try:
    C = load_config(CONFIG)
except FileNotFoundError:
    sys.exit(f"[FATAL] нет конфига {CONFIG}")

def need(k):
    v = C.get(k)
    if not v:
        sys.exit(f"[FATAL] в {CONFIG} нет {k}")
    return v

TOKEN      = need("TG_BOT_TOKEN")
ALLOWED_ID = int(need("TG_ALLOWED_ID"))
WORKDIR    = os.path.expanduser(C.get("WORKDIR", "~"))
CLAUDE_BIN = os.path.expanduser(C.get("CLAUDE_BIN", "~/.local/bin/claude"))
PERM_MODE  = C.get("PERMISSION_MODE", "bypassPermissions")
EFFORT     = C.get("EFFORT", "high")          # low|medium|high|xhigh|max
MODEL      = C.get("CLAUDE_MODEL", "")
TIMEOUT    = int(C.get("CLAUDE_TIMEOUT", "1800"))
API        = f"https://api.telegram.org/bot{TOKEN}"
INCOMING   = os.path.expanduser("~/dev/tgbridge/incoming")
OUTGOING   = os.path.expanduser("~/dev/tgbridge/outgoing")
TGSEND     = os.path.expanduser("~/dev/tgbridge/tgsend")
os.makedirs(INCOMING, exist_ok=True)
os.makedirs(OUTGOING, exist_ok=True)
LONG_LIMIT   = int(C.get("LONG_LIMIT", "6000"))          # длиннее -> ответ файлом
BASH_TIMEOUT = int(C.get("BASH_TIMEOUT", "30"))
BASH_ALLOW = set(C.get("BASH_ALLOW",
    "ls pwd cat head tail grep rg find echo stat file wc du df ps nvidia-smi "
    "git python python3 pip conda which whoami hostname date uptime env printenv "
    "chmod mkdir touch cp ln tree tgsend").split())
BASH_DENY  = {"rm","mv","dd","mkfs","shutdown","reboot","halt","poweroff","sudo","su",
             "kill","pkill","killall","chown","chgrp","shred","eval","exec",":"}
bash_cwd   = WORKDIR
WHISPER_BIN   = os.path.expanduser(C.get("WHISPER_BIN", "/Users/den/dev/anaconda/anaconda3/envs/ml13/bin/whisper"))
WHISPER_MODEL = C.get("WHISPER_MODEL", "small")          # tiny|base|small|medium|large|turbo
WHISPER_LANG  = C.get("WHISPER_LANG", "ru")              # ru|en|auto
SYS_PROMPT = (
    "КОНТЕКСТ ЗАПУСКА: ты — Claude Code, запущен HEADLESS через telegram-мост (tgbridge на ноутбуке владельца), "
    "а НЕ в интерактивном терминале и не напрямую. Каждое сообщение владельца = задача; твой ответ уходит ему "
    "сообщением в Telegram (личный чат; ты — его личный ассистент с доступом к его машине: bash, ssh, файлы).\n"
    "ФОРМАТ ОТВЕТА: пиши обычным MARKDOWN (**жирный**, ~~зачёркнутый~~, `код`, ```блоки``` с языком, • списки, "
    "[ссылки](url)) — НЕ пиши сырые HTML-теги, форматирование конвертируется автоматически. Без md-таблиц, ASCII-арта "
    "и обилия #заголовков (Telegram их не покажет). Пиши компактно — это читают с телефона. Если результат — большой "
    "скрипт/файл или очень длинный текст, сохрани на диск и пришли через tgsend как файл, а в чат дай короткое резюме.\n"
    f"ФАЙЛЫ: чтобы отправить владельцу файл с диска — запусти в bash: {TGSEND} <абсолютный_путь> [подпись] "
    "(файл придёт ему в чат). Не вставляй бинарь/base64 в текст; файлы шли только по запросу. Присланные им "
    "картинки/файлы уже сохранены на диск (путь в его сообщении) — открывай через Read.\n"
    "ВЗАИМОДЕЙСТВИЕ: интерактивных кнопок/подтверждений нет — нужно уточнение, просто спроси текстом. "
    "Инструменты исполняются автоматически."
)
EXTRA_PROMPT_FILE = os.path.expanduser("~/.config/tgbridge/system_prompt.txt")

def full_sysprompt():
    """Встроенный промпт + опциональные инструкции владельца (читаются на лету, без рестарта)."""
    extra = ""
    try:
        if os.path.isfile(EXTRA_PROMPT_FILE):
            extra = open(EXTRA_PROMPT_FILE, encoding="utf-8").read().strip()
    except Exception as e:
        log("sysprompt extra err", repr(e))
    return SYS_PROMPT + ("\n\nДОП. ИНСТРУКЦИИ ВЛАДЕЛЬЦА:\n" + extra if extra else "")

# ---------- state ----------
session_id  = None            # текущая claude-сессия (для --resume)
last_data   = None            # последний json от claude (для /context)
total_cost  = 0.0
claude_lock = threading.Lock()  # сериализуем все запуски агента
loop_stop   = threading.Event()
loop_thread = None

def log(*a):
    print(time.strftime("%Y-%m-%d %H:%M:%S"), *a, flush=True)

# ---------- telegram ----------
def tg(method, **params):
    data = urllib.parse.urlencode({k: v for k, v in params.items() if v is not None}).encode()
    try:
        with urllib.request.urlopen(f"{API}/{method}", data=data, timeout=70) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        log("TG HTTP", method, e.code, e.read()[:200])
    except Exception as e:
        log("TG err", method, repr(e))
    return None

def md_to_html(text):
    """Markdown агента -> безопасный Telegram-HTML (b, code, pre, a)."""
    stash = []
    def keep(h):
        stash.append(h); return f"\x00{len(stash)-1}\x00"
    def fence(m):
        lang = (m.group(1) or "").strip()
        cls = f' class="language-{lang}"' if lang else ""
        return keep(f"<pre><code{cls}>{_html.escape(m.group(2))}</code></pre>")
    text = re.sub(r"```([^\n`]*)\n(.*?)```", fence, text, flags=re.S)          # код-блоки
    text = re.sub(r"`([^`\n]+)`", lambda m: keep(f"<code>{_html.escape(m.group(1))}</code>"), text)
    text = _html.escape(text)                                                  # экранируем остальное
    text = re.sub(r"\[([^\]]+)\]\(([^)\s]+)\)", lambda m: f'<a href="{m.group(2)}">{m.group(1)}</a>', text)
    text = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", text, flags=re.S)            # **жирный**
    text = re.sub(r"__(.+?)__", r"<b>\1</b>", text, flags=re.S)
    text = re.sub(r"~~(.+?)~~", r"<s>\1</s>", text, flags=re.S)                # ~~зачёркнутый~~
    text = re.sub(r"(?m)^#{1,6}\s*(.+?)\s*$", r"<b>\1</b>", text)             # заголовки
    text = re.sub(r"(?m)^(\s*)[-*]\s+", r"\1• ", text)                        # буллеты
    return re.sub(r"\x00(\d+)\x00", lambda m: stash[int(m.group(1))], text)   # вернуть код

def _chunks(text, limit=3500):
    out, cur = [], ""
    for line in text.split("\n"):
        if cur and len(cur) + len(line) + 1 > limit:
            out.append(cur); cur = ""
        cur = (cur + "\n" + line) if cur else line
        while len(cur) > limit:
            out.append(cur[:limit]); cur = cur[limit:]
    if cur:
        out.append(cur)
    return out

def send(chat_id, text):
    text = text if (text and text.strip()) else "(пустой ответ)"
    for chunk in _chunks(text):
        r = tg("sendMessage", chat_id=chat_id, text=md_to_html(chunk),
               parse_mode="HTML", disable_web_page_preview="true")
        if not (r and r.get("ok")):                        # HTML не распарсился -> plain, чтобы не потерять
            tg("sendMessage", chat_id=chat_id, text=chunk)

def send_reply(chat_id, text):
    """Ответ агента: очень длинный -> файлом, иначе обычным сообщением."""
    text = text if (text and text.strip()) else "(пустой ответ)"
    if len(text) > LONG_LIMIT:
        path = os.path.join(OUTGOING, f"answer_{int(time.time())}.md")
        try:
            with open(path, "w", encoding="utf-8") as f:
                f.write(text)
            send(chat_id, "📄 Ответ длинный — присылаю файлом.\n\n" + " ".join(text[:280].split()) + "…")
            subprocess.run([TGSEND, path, "ответ Claude"], timeout=60)
            return
        except Exception as e:
            log("send_reply file err", repr(e))
    send(chat_id, text)

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
    dest = os.path.join(INCOMING, f"{int(time.time())}_{file_id[-8:]}{ext}")
    try:
        urllib.request.urlretrieve(f"https://api.telegram.org/file/bot{TOKEN}/{fp}", dest)
        return dest
    except Exception as e:
        log("download err", repr(e))
        return None

def transcribe(path):
    """Распознать голос через whisper (env ml13). Вернуть текст или None."""
    if not os.path.isfile(WHISPER_BIN):
        log("whisper not found:", WHISPER_BIN); return None
    env = dict(os.environ, PATH=os.path.dirname(WHISPER_BIN) + ":" + os.environ.get("PATH", ""))
    cmd = [WHISPER_BIN, path, "--model", WHISPER_MODEL, "--output_format", "txt",
           "--output_dir", os.path.dirname(path), "--fp16", "False", "--verbose", "False"]
    if WHISPER_LANG and WHISPER_LANG.lower() != "auto":
        cmd += ["--language", WHISPER_LANG]
    try:
        subprocess.run(cmd, env=env, capture_output=True, text=True, timeout=600)  # 1-й раз качает модель
    except Exception as e:
        log("whisper err", repr(e)); return None
    txt = os.path.splitext(path)[0] + ".txt"
    try:
        return open(txt, encoding="utf-8").read().strip() if os.path.isfile(txt) else None
    except Exception:
        return None

# ---------- claude ----------
def run_claude(prompt, resume=None):
    """Запустить headless claude. Возврат (текст, data|None)."""
    cmd = [CLAUDE_BIN, "-p", prompt, "--output-format", "json",
           "--permission-mode", PERM_MODE, "--effort", EFFORT,
           "--append-system-prompt", full_sysprompt()]
    if MODEL:  cmd += ["--model", MODEL]
    if resume: cmd += ["--resume", resume]
    try:
        p = subprocess.run(cmd, cwd=WORKDIR, capture_output=True, text=True, timeout=TIMEOUT)
    except subprocess.TimeoutExpired:
        return f"⏱️ Агент не уложился в {TIMEOUT}s.", None
    except Exception as e:
        return f"⚠️ Не смог запустить агента: {e}", None
    try:
        data = json.loads(p.stdout.strip())
    except Exception:
        return (p.stdout.strip() or p.stderr.strip() or "(пустой ответ)")[:3900], None
    if data.get("is_error"):
        return "⚠️ Ошибка агента: " + str(data.get("result", ""))[:1500], data
    return data.get("result", ""), data

def fmt_context(d):
    u = d.get("usage", {}) or {}
    ctx = (u.get("input_tokens", 0) + u.get("cache_read_input_tokens", 0)
           + u.get("cache_creation_input_tokens", 0))
    win = max((m.get("contextWindow", 0) for m in d.get("modelUsage", {}).values()), default=200000)
    pct = 100 * ctx / win if win else 0
    return (f"🧮 Контекст (последний ход): ~{ctx:,} / {win:,} токенов ({pct:.1f}%)\n"
            f"≈ по API-тарифу: ${total_cost:.4f} (справочно; с подписки НЕ списывается)")

def status_text():
    running = loop_thread is not None and loop_thread.is_alive() and not loop_stop.is_set()
    return ("🟢 tgbridge живёт\n"
            f"диалог: {'активен ' + session_id[:8] + '…' if session_id else 'новый (пусто)'}\n"
            f"стоимость: ${total_cost:.4f}\n"
            f"loop: {'работает' if running else 'нет'}\n"
            f"агент занят: {'да' if claude_lock.locked() else 'нет'}\n"
            f"effort: {EFFORT}\n"
            f"workdir: {WORKDIR}")

HELP = (
    "🤖 tgbridge — Claude Code по телеге\n\n"
    "Просто пиши сообщение — оно уйдёт агенту (со всеми инструментами: bash, ssh, файлы).\n\n"
    "Команды:\n"
    "/reset — начать новый диалог\n"
    "/status — статус моста\n"
    "/context — заполнение контекста + стоимость\n"
    "/btw <вопрос> — побочный вопрос, НЕ трогая основной диалог\n"
    "/loop <сек> <промпт> — повторять промпт каждые N сек\n"
    "/loop stop  (или /stop) — остановить loop\n"
    "/compact и другие claude-слэши — пробрасываются агенту\n"
    "!<команда> — быстрый bash из белого списка (ls, cat, grep, git, cd, chmod…), без rm/mv\n\n"
    "🎙 Пришли голосовое — распознаю (whisper) и выполню как команду.\n"
    "📎 Пришли картинку или файл — агент их посмотрит (через Read).\n"
    "Попроси «пришли мне <файл>» — агент отправит файл в чат (длинные ответы тоже приходят файлом).")

# ---------- loop ----------
def start_loop(chat_id, interval, prompt):
    global loop_thread
    loop_stop.set(); time.sleep(0.05); loop_stop.clear()   # сбросить прошлый
    def runner():
        send(chat_id, f"🔁 Loop запущен: каждые {interval}s.\nОстановить: /loop stop")
        while not loop_stop.is_set():
            res, _ = run_claude(prompt, resume=None)        # каждый тик — свежая сессия
            if loop_stop.is_set(): break
            send(chat_id, "🔁 " + res)
            loop_stop.wait(interval)
        send(chat_id, "⏹️ Loop остановлен.")
    loop_thread = threading.Thread(target=runner, daemon=True)
    loop_thread.start()

def handle_loop(chat_id, text):
    parts = text.split(maxsplit=2)
    if len(parts) >= 2 and parts[1].lower() in ("stop", "off"):
        loop_stop.set(); return
    if len(parts) < 3:
        send(chat_id, "Использование: /loop <секунды> <промпт>\nОстановить: /loop stop"); return
    try:
        interval = max(30, int(parts[1]))
    except ValueError:
        send(chat_id, "Первый аргумент — секунды (число ≥ 30)."); return
    start_loop(chat_id, interval, parts[2])

# ---------- ! bash (ограниченный белый список, как в jupyter) ----------
def handle_bang(chat_id, cmd):
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

# ---------- dispatch ----------
def handle(chat_id, text):
    global session_id, last_data, total_cost
    t = text.strip()
    low = t.lower()

    if low in ("/start", "/help"):
        send(chat_id, HELP); return
    if low == "/reset":
        session_id = None; send(chat_id, "🔄 Начал новый диалог."); return
    if low == "/status":
        send(chat_id, status_text()); return
    if low == "/context":
        send(chat_id, fmt_context(last_data) if last_data else "Пока нет данных — напиши что-нибудь агенту."); return
    if low in ("/stop", "/cancel"):
        loop_stop.set(); send(chat_id, "⏹️ Остановил loop (если был)."); return
    if low.startswith("/loop"):
        handle_loop(chat_id, t); return
    if low.startswith("/btw"):
        q = t[4:].strip()
        if not q:
            send(chat_id, "Использование: /btw <вопрос> — побочный вопрос, не трогая основной диалог."); return
        with Typing(chat_id):
            with claude_lock:
                res, _ = run_claude(q, resume=None)         # отдельная сессия, session_id НЕ меняем
        send_reply(chat_id, "💬 (побочный)\n" + res); return

    # обычное сообщение ИЛИ проброс claude-слэша (/compact, /cost, кастомные)
    with Typing(chat_id):
        with claude_lock:
            res, data = run_claude(t, resume=session_id)
            if data and data.get("session_id"):
                session_id = data["session_id"]
                last_data  = data
                total_cost += data.get("total_cost_usd", 0) or 0
    send_reply(chat_id, res)

def safe_handle(chat_id, text):
    try:
        handle(chat_id, text)
    except Exception as e:
        log("handle err", repr(e)); send(chat_id, f"⚠️ Внутренняя ошибка: {e}")

# ---------- альбомы (media groups) ----------
mg_lock = threading.Lock()
media_groups = {}   # media_group_id -> {photos, caption, chat_id, timer}

def flush_group(mgid):
    with mg_lock:
        grp = media_groups.pop(mgid, None)
    if not grp:
        return
    paths = [p for p in (download_file(fid, "img.jpg") for fid in grp["photos"]) if p]
    if not paths:
        send(grp["chat_id"], "⚠️ Не смог скачать картинки альбома."); return
    cap = grp["caption"].strip()
    prompt = (f"[Пользователь прислал {len(paths)} изображений (альбом), сохранены: {', '.join(paths)}]\n"
              + (cap or "Посмотри ВСЕ изображения (через Read) и ответь по ним."))
    log(f"album {mgid}: {len(paths)} img")
    safe_handle(grp["chat_id"], prompt)

def buffer_group(chat_id, mgid, file_id, caption):
    with mg_lock:
        grp = media_groups.setdefault(mgid, {"photos": [], "caption": "", "chat_id": chat_id, "timer": None})
        grp["photos"].append(file_id)
        if caption:
            grp["caption"] = caption
        if grp["timer"]:
            grp["timer"].cancel()
        grp["timer"] = threading.Timer(2.0, flush_group, args=[mgid])
        grp["timer"].daemon = True
        grp["timer"].start()

# ---------- контекст сообщения (reply / forward) ----------
def _uname(u):
    if not u:
        return "неизвестно"
    n = " ".join(x for x in (u.get("first_name"), u.get("last_name")) if x).strip()
    return (n or "?") + (f" (@{u['username']})" if u.get("username") else "")

def forward_source(msg):
    """Кто оригинальный автор пересланного сообщения, либо None."""
    fo = msg.get("forward_origin")
    if fo:
        t = fo.get("type")
        if t == "user":        return _uname(fo.get("sender_user"))
        if t == "hidden_user": return fo.get("sender_user_name", "скрытый пользователь")
        if t == "channel":     return "канал «" + fo.get("chat", {}).get("title", "?") + "»"
        if t == "chat":        return "чат «" + fo.get("sender_chat", {}).get("title", "?") + "»"
    if msg.get("forward_from"):        return _uname(msg["forward_from"])
    if msg.get("forward_sender_name"): return msg["forward_sender_name"]
    if msg.get("forward_from_chat"):   return "канал/чат «" + msg["forward_from_chat"].get("title", "?") + "»"
    return None

def context_prefix(msg):
    """Префикс с контекстом: переслано откуда / ответ на какое сообщение."""
    parts = []
    fs = forward_source(msg)
    if fs:
        parts.append(f"[ПЕРЕСЛАННОЕ сообщение — это НЕ слова владельца, переслано от: {fs}. Учитывай как контекст.]")
    rt = msg.get("reply_to_message")
    if rt:
        who = "ты (твой прошлый ответ)" if (rt.get("from") or {}).get("is_bot") else _uname(rt.get("from"))
        q = rt.get("text") or rt.get("caption") or ("[медиа/файл]" if (rt.get("photo") or rt.get("document")) else "")
        if len(q) > 600:
            q = q[:600] + "…"
        parts.append(f"[Владелец отвечает (reply) на сообщение от «{who}»: «{q}»]")
    return ("\n".join(parts) + "\n") if parts else ""

# ---------- main loop ----------
def main():
    me = tg("getMe")
    who = me["result"]["username"] if me and me.get("ok") else "??? (проверь токен!)"
    log(f"tgbridge up | bot=@{who} | allowed_id={ALLOWED_ID} | workdir={WORKDIR} | claude={CLAUDE_BIN}")
    tg("deleteWebhook")                # на случай ранее выставленного вебхука
    offset = None
    while True:
        resp = tg("getUpdates", offset=offset, timeout=50)
        if not resp or not resp.get("ok"):
            time.sleep(3); continue
        for upd in resp["result"]:
            offset = upd["update_id"] + 1
            msg = upd.get("message") or upd.get("edited_message")
            if not msg:
                continue
            frm = (msg.get("from") or {}).get("id")
            chat_id = msg["chat"]["id"]
            text = msg.get("text", "")
            if frm != ALLOWED_ID:
                log(f"IGNORED from id={frm} chat={chat_id}: {text[:60]!r}")
                continue
            if text.startswith("!"):                         # быстрый bash из белого списка (не через агента)
                threading.Thread(target=handle_bang, args=(chat_id, text[1:].strip()), daemon=True).start()
                continue
            mgid = msg.get("media_group_id")
            if mgid and msg.get("photo"):                    # альбом -> буферизуем, отдадим одним ходом
                buffer_group(chat_id, mgid, msg["photo"][-1]["file_id"], (msg.get("caption") or "").strip())
                continue
            prompt = text
            if not prompt:                                   # не текст -> картинка/файл?
                cap = (msg.get("caption") or "").strip()
                if msg.get("photo"):
                    path = download_file(msg["photo"][-1]["file_id"], "img.jpg")
                    if not path:
                        send(chat_id, "⚠️ Не смог скачать картинку."); continue
                    prompt = (f"[Пользователь прислал изображение, сохранено: {path}]\n"
                              + (cap or "Посмотри изображение (через Read) и ответь по нему."))
                elif msg.get("document"):
                    doc = msg["document"]
                    path = download_file(doc["file_id"], doc.get("file_name", ""))
                    if not path:
                        send(chat_id, "⚠️ Не смог скачать файл."); continue
                    prompt = (f"[Пользователь прислал файл, сохранён: {path} (имя: {doc.get('file_name','?')})]\n"
                              + (cap or "Посмотри/обработай этот файл и ответь."))
                elif msg.get("voice") or msg.get("video_note") or msg.get("audio"):
                    v = msg.get("voice") or msg.get("video_note") or msg.get("audio")
                    path = download_file(v["file_id"], "voice.ogg")
                    if not path:
                        send(chat_id, "⚠️ Не смог скачать голосовое."); continue
                    with Typing(chat_id):
                        heard = transcribe(path)
                    if not heard:
                        send(chat_id, "⚠️ Не распознал голос (whisper). Проверь WHISPER_BIN в конфиге."); continue
                    send(chat_id, "🎙 услышал: «" + heard + "»")
                    prompt = (cap + "\n" if cap else "") + heard
                else:
                    send(chat_id, "Умею текст, голос, картинки и файлы (и могу присылать файлы по запросу)."); continue
            prompt = context_prefix(msg) + prompt          # reply/forward-контекст
            log(f"msg from {frm}: {prompt[:80]!r}")
            threading.Thread(target=safe_handle, args=(chat_id, prompt), daemon=True).start()

if __name__ == "__main__":
    main()
