#!/usr/bin/env python3
"""Напоминания о платежах -> Telegram владельцу.

Логика:
  * за 2 дня до платежа — напоминание приходит всегда;
  * за 1 день — только если платёж ещё НЕ отмечен оплаченным.

Запускается через launchd: com.local.payments-reminder (ежедневно).

CLI:
  payments-reminder.py                  штатный прогон (шлёт напоминания)
  payments-reminder.py --dry            то же, но печатает в консоль
  payments-reminder.py --status         таблица статусов за текущий месяц
  payments-reminder.py --paid Ипотека   отметить оплаченным (поиск по части имени)
  payments-reminder.py --paid all       отметить всё, что напоминалось сейчас
  payments-reminder.py --unpaid Ипотека снять отметку
  payments-reminder.py --days 30        обзор на N дней вперёд
  payments-reminder.py --move Вольво 17.02   перенести ближайшее вхождение
  payments-reminder.py --move Вольво reset   снять переносы
  payments-reminder.py --paid ЖКХ --amount 26500   отметить с фактической суммой
  payments-reminder.py --report            факт за текущий месяц
  payments-reminder.py --report 09.2026    факт за месяц
  payments-reminder.py --report 2026       факт за год
  payments-reminder.py --spend "Название" 15000 [--date 29.08] [--note "..."] [--cur USD]
                                           разовая трата сразу в журнал

Журнал фактических трат: payments-log.csv (date,name,fact,currency,plan,note)
"""
import calendar
import csv
import datetime as dt
import json
import os
import sys
import urllib.parse
import urllib.request

HOME = os.path.expanduser("~")
SCHEDULE = os.path.join(HOME, "dev/tgbridge/payments.json")
PAIDFILE = os.path.join(HOME, "dev/tgbridge/payments-paid.json")
LOGFILE = os.path.join(HOME, "dev/tgbridge/payments-log.csv")   # журнал ФАКТИЧЕСКИХ трат
CFG = os.path.join(HOME, ".config/tgbridge/config.env")

RU_DAYS = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]


# ---------- инфраструктура ----------

def cfg(key):
    try:
        with open(CFG, encoding="utf-8") as f:
            for line in f:
                if line.startswith(key + "="):
                    return line.split("=", 1)[1].strip().strip("\"'")
    except OSError:
        pass
    return ""


def load_schedule():
    with open(SCHEDULE, encoding="utf-8") as f:
        return json.load(f)


def load_paid():
    try:
        with open(PAIDFILE, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def save_paid(d):
    with open(PAIDFILE, "w", encoding="utf-8") as f:
        json.dump(d, f, ensure_ascii=False, indent=2)


def log_rows():
    """Журнал как список словарей. Пустой, если файла ещё нет."""
    try:
        with open(LOGFILE, encoding="utf-8", newline="") as f:
            return list(csv.DictReader(f))
    except OSError:
        return []


def log_save(rows):
    with open(LOGFILE, "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["date", "name", "fact", "currency", "plan", "note"])
        w.writeheader()
        w.writerows(sorted(rows, key=lambda r: (r["date"], r["name"])))


def log_add(name, date, fact, currency, plan, note=""):
    """Записать факт оплаты. Повторная отметка того же платежа перезаписывает строку."""
    rows = [r for r in log_rows()
            if not (r["name"] == name and r["date"] == date.isoformat())]
    rows.append({"date": date.isoformat(), "name": name, "fact": "%g" % float(fact),
                 "currency": currency, "plan": "%g" % float(plan), "note": note})
    log_save(rows)


def log_remove(name, date):
    rows = [r for r in log_rows()
            if not (r["name"] == name and r["date"] == date.isoformat())]
    log_save(rows)


def send(text):
    token, chat = cfg("TG_BOT_TOKEN"), cfg("TG_ALLOWED_ID")
    if not token or not chat:
        print("нет TG_BOT_TOKEN/TG_ALLOWED_ID в " + CFG, file=sys.stderr)
        return False
    data = urllib.parse.urlencode(
        {"chat_id": chat, "text": text, "parse_mode": "HTML"}).encode()
    url = "https://api.telegram.org/bot" + token + "/sendMessage"
    with urllib.request.urlopen(url, data=data, timeout=30) as r:
        return r.status == 200


# ---------- даты ----------

def clamp_day(year, month, day):
    """31-е число в феврале -> последний день месяца."""
    return min(day, calendar.monthrange(year, month)[1])


def is_window(p):
    """Платёж-окно: сделать в любой день в диапазоне, напоминаем каждый день."""
    return p.get("period") == "window"


def is_daily(p):
    """Ежедневное напоминание — до отключения (active: false)."""
    return p.get("period") == "daily"


def win_range(p):
    return (dt.date.fromisoformat(str(p["date_from"])),
            dt.date.fromisoformat(str(p["date_to"])))


def base_due_in(p, year, month):
    """Плановая дата платежа в месяце, без учёта переносов (или None)."""
    period = p.get("period", "monthly")
    if period == "monthly":
        return dt.date(year, month, clamp_day(year, month, int(p["day"])))
    if period == "yearly" and int(p["month"]) == month:
        return dt.date(year, month, clamp_day(year, month, int(p["day"])))
    if period == "once":
        d = dt.date.fromisoformat(str(p["date"]))
        return d if (d.year, d.month) == (year, month) else None
    if period == "window":
        d = win_range(p)[1]
        return d if (d.year, d.month) == (year, month) else None
    return None


def due_date_in(p, year, month):
    """Фактическая дата платежа в месяце с учётом разовых переносов.

    reschedule = {"2027-02-10": "2027-02-17"} — плановая дата -> фактическая.
    Якорь (day/month) не трогаем: перенос действует на одно вхождение, в следующем
    году ТО снова планируется от исходной даты.
    """
    resc = p.get("reschedule") or {}
    base = base_due_in(p, year, month)
    if base is not None:
        moved = resc.get(base.isoformat())
        if not moved:
            return base
        m = dt.date.fromisoformat(moved)
        return m if (m.year, m.month) == (year, month) else None
    for src, dst in resc.items():          # перенос мог затащить платёж из соседнего месяца
        m = dt.date.fromisoformat(dst)
        if (m.year, m.month) != (year, month):
            continue
        s = dt.date.fromisoformat(src)
        if base_due_in(p, s.year, s.month) == s:
            return m
    return None


def moved_from(p, date):
    """Если date — результат переноса, вернуть исходную плановую дату."""
    for src, dst in (p.get("reschedule") or {}).items():
        if dst == date.isoformat():
            return dt.date.fromisoformat(src)
    return None


def due_on(p, date):
    if not p.get("active", True):
        return False
    return due_date_in(p, date.year, date.month) == date


LOOKBACK = 45          # насколько далеко назад считаем платёж «ещё живым» (просроченным)


def last_due(p, today, lookback=LOOKBACK):
    """Последняя наступившая дата платежа (<= today) в пределах lookback дней."""
    if is_window(p):
        return win_range(p)[1]
    for delta in range(0, lookback + 1):
        d = today - dt.timedelta(days=delta)
        if due_on(p, d):
            return d
    return None


def nearest_due(p, today, window=7, paid=None):
    """Ближайшая дата платежа: сначала неоплаченные, иначе просто ближайшая."""
    if is_window(p):                       # для окна «дата» = крайний срок
        df, dl = win_range(p)
        if df - dt.timedelta(days=2) <= today <= dl + dt.timedelta(days=60):
            return dl
        return None
    cands = []
    for delta in range(-LOOKBACK, window + 1):
        d = today + dt.timedelta(days=delta)
        if due_on(p, d):
            cands.append(d)
    if not cands:
        return None
    paid = paid or {}
    unpaid = [d for d in cands if paid.get(p["name"]) != d.isoformat()]
    pool = unpaid or cands
    return min(pool, key=lambda d: abs((d - today).days))


def fmt_money(x):
    return ("%.0f" % x if float(x).is_integer() else "%.2f" % x).replace(
        ",", "").rjust(1)


def money(x):
    s = "{:,.0f}".format(float(x)) if float(x) == int(float(x)) else "{:,.2f}".format(float(x))
    return s.replace(",", " ")


def totals_str(by_cur):
    return " + ".join("<b>%s %s</b>" % (money(v), k) for k, v in by_cur.items())


def ru_date(d):
    return "%s (%s)" % (d.strftime("%d.%m"), RU_DAYS[d.weekday()])


# ---------- режимы ----------

def do_remind(dry=False):
    sched = load_schedule()
    payments = [p for p in sched.get("payments", []) if p.get("active", True)]
    paid = load_paid()
    today = dt.date.today()

    blocks = []
    regular = [p for p in payments if not is_window(p)]
    windows = [p for p in payments if is_window(p)]

    # ПРОСРОЧЕННЫЕ — напоминаем каждый день, пока не отмечены оплаченными
    since = dt.date.fromisoformat(str(sched.get("tracking_since", "1970-01-01")))
    dlines, dby = [], {}
    for p in regular:
        d = last_due(p, today)
        if d is None or d >= today or d < since:
            continue
        if paid.get(p["name"]) == d.isoformat():
            continue
        cur = p.get("currency", sched.get("currency_default", "RUB"))
        dby[cur] = dby.get(cur, 0) + float(p["amount"])
        note = " — <i>%s</i>" % p["note"] if p.get("note") else ""
        dlines.append("• <b>%s</b> — %s %s · срок был %s (%d дн. назад)%s"
                      % (p["name"], money(p["amount"]), cur,
                         d.strftime("%d.%m"), (today - d).days, note))
    if dlines:
        blocks.append("🔴 <b>Просрочено</b>\n\n%s\n\n➡️ Нужно: %s"
                      % ("\n".join(dlines), totals_str(dby)))

    # окна: напоминаем КАЖДЫЙ день, пока не отмечено сделанным
    wlines, wby = [], {}
    for p in windows:
        df, dl = win_range(p)
        if paid.get(p["name"]) == dl.isoformat():
            continue
        if today < df - dt.timedelta(days=1):
            continue
        cur = p.get("currency", sched.get("currency_default", "RUB"))
        wby[cur] = wby.get(cur, 0) + float(p["amount"])
        left = (dl - today).days
        if today < df:
            when = "окно открывается завтра, %s–%s" % (df.strftime("%d.%m"), dl.strftime("%d.%m"))
        elif today > dl:
            when = "⚠️ срок вышел %s" % dl.strftime("%d.%m")
        else:
            when = "до %s, осталось %d дн." % (dl.strftime("%d.%m"), left)
        note = " — <i>%s</i>" % p["note"] if p.get("note") else ""
        wlines.append("• <b>%s</b> — %s %s · %s%s"
                      % (p["name"], money(p["amount"]), cur, when, note))
    if wlines:
        blocks.append("🗓 <b>Сделать в любой день</b>\n\n%s\n\n➡️ Отложить: %s"
                      % ("\n".join(wlines), totals_str(wby)))

    for offset, title in ((2, "Через 2 дня"), (1, "Завтра"), (0, "СЕГОДНЯ")):
        target = today + dt.timedelta(days=offset)
        due = [p for p in regular if due_on(p, target)]
        # отмеченное оплаченным не напоминаем ни на одном слоте: платёж можно закрыть
        # сильно заранее (ОСАГО за две недели), и «через 2 дня» по нему уже просто шум
        due = [p for p in due if paid.get(p["name"]) != target.isoformat()]
        if not due:
            continue
        by_cur, lines = {}, []
        for p in due:
            cur = p.get("currency", sched.get("currency_default", "RUB"))
            by_cur[cur] = by_cur.get(cur, 0) + float(p["amount"])
            note = " — <i>%s</i>" % p["note"] if p.get("note") else ""
            lines.append("• <b>%s</b> — %s %s%s" % (p["name"], money(p["amount"]), cur, note))
        icon = "🔔" if offset == 0 else "💳"
        blocks.append("%s <b>%s, %s</b>\n\n%s\n\n➡️ Перевести: %s"
                      % (icon, title, ru_date(target), "\n".join(lines), totals_str(by_cur)))

    if not blocks:
        if dry:
            print("на ближайшие 2 дня платежей нет — молчим")
        return

    msg = "\n\n———\n\n".join(blocks)
    msg += "\n\n<i>Оплатил — просто напиши, отмечу.</i>"
    print(msg) if dry else send(msg)


def do_status(dry=False):
    sched = load_schedule()
    payments = [p for p in sched.get("payments", []) if p.get("active", True)]
    paid = load_paid()
    today = dt.date.today()

    rows, left = [], {}
    items = []
    for p in payments:
        d = due_date_in(p, today.year, today.month)
        if d:
            items.append((d, p))
    items.sort(key=lambda x: x[0])


    for d, p in items:
        cur = p.get("currency", sched.get("currency_default", "RUB"))
        is_paid = paid.get(p["name"]) == d.isoformat()
        if is_window(p):
            df, dl = win_range(p)
            when = "%s–%s" % (df.strftime("%d.%m"), dl.strftime("%d.%m"))
            if is_paid:
                icon, st = "🟢", "сделано"
            elif today > dl:
                icon, st = "🔴", "срок вышел"
            elif today >= df:
                icon, st = "🟡", "можно сейчас, осталось %d дн." % (dl - today).days
            else:
                icon, st = "⚪", "окно через %d дн." % (df - today).days
        else:
            when = d.strftime("%d.%m")
            src = moved_from(p, d)
            if src:                                    # видно, что дата сдвинута вручную
                when += " (перенесено с %s)" % src.strftime("%d.%m")
            if is_paid:
                icon, st = "🟢", "оплачено"
            elif d < today:
                icon, st = "🔴", "просрочено"
            elif d == today:
                icon, st = "🟡", "сегодня"
            else:
                icon, st = "⚪", "через %d дн." % (d - today).days
        if not is_paid:
            left[cur] = left.get(cur, 0) + float(p["amount"])
        rows.append("%s <b>%s</b> — %s %s · %s · %s"
                    % (icon, p["name"], money(p["amount"]), cur, when, st))

    head = "📋 <b>Платежи за %s</b>\n\n" % today.strftime("%m.%Y")
    tail = ("\n\nОсталось оплатить: %s" % totals_str(left)) if left else "\n\n✅ Всё оплачено"
    msg = head + "\n".join(rows) + tail
    print(msg) if dry else send(msg)


def do_paid(query, mark=True, dry=False, fact=None):
    sched = load_schedule()
    payments = [p for p in sched.get("payments", []) if p.get("active", True)]
    paid = load_paid()
    today = dt.date.today()

    if query.lower() in ("all", "все", "всё"):
        win = 7                                  # «всё» = только то, что реально на подходе
        targets = [p for p in payments if nearest_due(p, today, win) is not None]
    else:
        win = 400                                # названный платёж можно закрыть заранее (ОСАГО за 2 недели)
        q = query.lower()
        targets = [p for p in payments if q in p["name"].lower()]

    if not targets:
        print("не нашёл платёж по запросу: " + query)
        return

    done = []
    for p in targets:
        d = nearest_due(p, today, win)
        if d is None:
            continue
        plan = float(p["amount"])
        amount = plan if fact is None else float(fact)
        cur = p.get("currency", sched.get("currency_default", "RUB"))
        if mark:
            paid[p["name"]] = d.isoformat()
            if not dry:
                log_add(p["name"], d, amount, cur, plan)
        else:
            paid.pop(p["name"], None)
            if not dry:
                log_remove(p["name"], d)
        done.append("%s (%s%s)" % (p["name"], d.strftime("%d.%m"),
                                   "" if amount == plan else ", факт %s %s" % (money(amount), cur)))

    if not done:
        print("нашёл платёж, но ближайшей даты нет — отмечать нечего: "
              + ", ".join(x["name"] for x in targets))
        return
    if not dry:
        save_paid(paid)
    verb = "отмечено оплаченным" if mark else "отметка снята"
    print("%s: %s" % (verb, ", ".join(done)))


def do_lookahead(days, dry=False):
    sched = load_schedule()
    payments = [p for p in sched.get("payments", []) if p.get("active", True)]
    today = dt.date.today()
    rows, by_cur = [], {}
    d = today + dt.timedelta(days=1)
    end = today + dt.timedelta(days=days)
    while d <= end:
        for p in payments:
            if due_on(p, d):
                cur = p.get("currency", sched.get("currency_default", "RUB"))
                by_cur[cur] = by_cur.get(cur, 0) + float(p["amount"])
                rows.append("• %s — <b>%s</b>, %s %s"
                            % (d.strftime("%d.%m"), p["name"], money(p["amount"]), cur))
        d += dt.timedelta(days=1)
    msg = ("💳 <b>Платежи на %d дн. вперёд</b>\n\n%s\n\nИтого: %s"
           % (days, "\n".join(rows), totals_str(by_cur))) if rows else \
          "💳 Платежей в ближайшие %d дн. нет." % days
    print(msg) if dry else send(msg)


def parse_date(text, today=None):
    """DD.MM | DD.MM.YYYY | YYYY-MM-DD. Без года — ближайшая такая дата в будущем."""
    today = today or dt.date.today()
    t = text.strip()
    try:
        if "-" in t:
            return dt.date.fromisoformat(t)
        parts = [int(x) for x in t.replace("/", ".").split(".")]
        if len(parts) == 3:
            day, month, year = parts
            return dt.date(year + 2000 if year < 100 else year, month, day)
        if len(parts) == 2:
            day, month = parts
            d = dt.date(today.year, month, day)
            return d if d >= today else dt.date(today.year + 1, month, day)
    except (ValueError, TypeError):
        pass
    return None


def do_move(query, when, dry=False):
    """Перенести ближайшее вхождение платежа на другую дату (запись в сервис и т.п.)."""
    sched = load_schedule()
    payments = [x for x in sched.get("payments", []) if x.get("active", True)]
    today = dt.date.today()
    q = query.lower()
    targets = [x for x in payments if q in x["name"].lower()]
    if len(targets) != 1:
        print("не нашёл платёж" if not targets
              else "запрос неоднозначный: " + ", ".join(x["name"] for x in targets))
        return
    p = targets[0]

    if when.lower() in ("reset", "сброс", "вернуть"):
        p.pop("reschedule", None)
        print("переносы сняты: %s" % p["name"])
    else:
        new = parse_date(when, today)
        if new is None:
            print("не понял дату: " + when)
            return
        if p.get("period") == "once":                  # разовому просто меняем дату
            old = p.get("date")
            p["date"] = new.isoformat()
            print("%s: %s -> %s" % (p["name"], old, new.isoformat()))
        else:
            base = None                                # ближайшее ПЛАНОВОЕ вхождение
            for delta in range(-LOOKBACK, 401):
                d = today + dt.timedelta(days=delta)
                if base_due_in(p, d.year, d.month) == d:
                    base = d
                    break
            if base is None:
                print("не нашёл ближайшее вхождение для " + p["name"])
                return
            resc = p.get("reschedule") or {}
            cutoff = (today - dt.timedelta(days=400)).isoformat()
            resc = {k: v for k, v in resc.items() if v >= cutoff}   # старые переносы не копим
            resc[base.isoformat()] = new.isoformat()
            p["reschedule"] = resc
            print("%s: %s -> %s (якорь %02d.%02d не тронут)"
                  % (p["name"], base.strftime("%d.%m.%Y"), new.strftime("%d.%m.%Y"),
                     base.day, base.month))

    if dry:
        print("(dry — не сохраняю)")
        return
    with open(SCHEDULE, "w", encoding="utf-8") as f:
        json.dump(sched, f, ensure_ascii=False, indent=2)


def do_spend(name, amount, when=None, note="", currency="RUB", dry=False):
    """Разовая трата, которой не было в плане: сразу в журнал, без записи в расписание."""
    d = parse_date(when) if when else dt.date.today()
    if d is None:
        print("не понял дату: " + str(when))
        return
    if not dry:
        log_add(name, d, float(amount), currency, float(amount), note)
    print("в расходы: %s — %s %s · %s%s"
          % (name, money(amount), currency, d.strftime("%d.%m.%Y"),
             " · " + note if note else ""))


def do_report(period=None, dry=False):
    """Сводка фактических трат: --report (текущий месяц) | 09.2026 | 2026."""
    rows = log_rows()
    if not rows:
        print("журнал пуст: " + LOGFILE)
        return
    today = dt.date.today()
    key = period or today.strftime("%m.%Y")
    if len(key) == 4:                                   # год целиком
        rows = [r for r in rows if r["date"][:4] == key]
        title = "за %s год" % key
    else:
        mm, yy = key.split(".")
        rows = [r for r in rows if r["date"][:7] == "%s-%s" % (yy, mm)]
        title = "за %s" % key
    if not rows:
        print("нет записей " + title)
        return

    by_cur, lines = {}, []
    for r in sorted(rows, key=lambda x: x["date"]):
        fact, plan = float(r["fact"]), float(r["plan"])
        by_cur[r["currency"]] = by_cur.get(r["currency"], 0) + fact
        delta = ""
        if fact != plan:
            delta = " (план %s, %+g)" % (money(plan), fact - plan)
        lines.append("• %s — <b>%s</b> — %s %s%s"
                     % (dt.date.fromisoformat(r["date"]).strftime("%d.%m"),
                        r["name"], money(fact), r["currency"], delta))
    text = ("📒 <b>Факт %s</b>\n\n%s\n\nИтого: %s"
            % (title, "\n".join(lines), totals_str(by_cur)))
    if dry:
        print(text)
    else:
        send(text)


def main():
    a = sys.argv[1:]
    dry = "--dry" in a

    if "--status" in a:
        return do_status(dry)
    fact = None
    if "--amount" in a:
        fact = float(a[a.index("--amount") + 1].replace(" ", "").replace(",", "."))
    if "--spend" in a:
        i = a.index("--spend")
        note = a[a.index("--note") + 1] if "--note" in a else ""
        when = a[a.index("--date") + 1] if "--date" in a else None
        cur = a[a.index("--cur") + 1] if "--cur" in a else "RUB"
        return do_spend(a[i + 1], a[i + 2], when, note, cur, dry)
    if "--report" in a:
        i = a.index("--report")
        arg = a[i + 1] if len(a) > i + 1 and not a[i + 1].startswith("--") else None
        return do_report(arg, dry)
    if "--paid" in a:
        return do_paid(a[a.index("--paid") + 1], True, dry, fact)
    if "--unpaid" in a:
        return do_paid(a[a.index("--unpaid") + 1], False, dry)
    if "--move" in a:
        i = a.index("--move")
        return do_move(a[i + 1], a[i + 2], dry)
    if "--days" in a:
        return do_lookahead(int(a[a.index("--days") + 1]), dry)
    return do_remind(dry)


if __name__ == "__main__":
    main()
