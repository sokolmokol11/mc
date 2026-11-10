"""
СТУПЕНЬ 1 — быстрое сито без аккаунта.

Смотрит публичные страницы t.me (название, описание, подписчики, последние ~20 постов)
и раскладывает тысячи ссылок на:
  ton      — точно связан с TON           → идёт на ступень 2
  maybe    — живой, но TON не определить  → идёт на ступень 2 (только чаты и каналы со скрытыми постами)
  not_ton  — живой, но про другое         → выкидываем
  dead     — мёртвый / пустой / не найден → выкидываем

Аккаунт не нужен, поэтому лимиты Telegram на аккаунт не тратятся.

Запуск (на VPS лучше в фоне, см. инструкцию):
  python3.11 prefilter.py                       — взять ссылки из input.txt (разделы [Крипто чаты] и т.п.)
  python3.11 prefilter.py channels.txt --as crypto_channel   — файл без заголовков
  python3.11 prefilter.py chats.txt --as crypto_chat
Можно перечислить несколько файлов. Прерванный запуск продолжится с того же места.

Результат:
  stage1/candidates.txt — что пойдёт на ступень 2 (формат как у input.txt)
  stage1/report.csv     — таблица по всем ссылкам с цифрами и причиной
"""
import argparse
import csv
import json
import random
import re
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path

import requests
from bs4 import BeautifulSoup

from common import (BASE, CATEGORIES, GAME_RE, TON_RE, pretty_hits, proxy_url,
                    read_links, topic_score)

# ============================================================
#                         НАСТРОЙКИ
# ============================================================
WORKERS = 3                 # сколько страниц открывать одновременно
PAUSE = (0.8, 1.6)          # пауза каждого потока между запросами, сек

TON_MIN_SCORE = 3           # очков TON, чтобы считать связанным с TON
DEAD_DAYS = 60              # канал без постов дольше — мёртвый
CHAT_MIN_MEMBERS = 50       # чат меньше — мёртвый
CHAT_MIN_ONLINE = 3         # совсем никого онлайн — мёртвый (точную активность проверит ступень 2)
# ============================================================

USE_ONLINE = True           # выключается флагом --no-online (утром/ночью онлайн низкий)

OUT = BASE / "stage1"
CACHE = OUT / "cache.jsonl"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/124.0 Safari/537.36",
    "Accept-Language": "en-US,en;q=0.9",
}

COUNT_RE = re.compile(r"(\d[\d\s .,]*[KMkm]?)\s+(subscribers?|members?|online)")
_local = threading.local()
_cache_lock = threading.Lock()


# ------------------------- сеть -----------------------------

def session():
    s = getattr(_local, "s", None)
    if s is None:
        s = requests.Session()
        s.headers.update(HEADERS)
        url = proxy_url()
        if url:
            s.proxies = {"http": url, "https": url}
        _local.s = s
    return s


def fetch(url):
    """Возвращает (статус, итоговый url, html) или None, если сеть так и не ответила."""
    for attempt in range(6):
        try:
            r = session().get(url, timeout=25)
        except requests.RequestException:
            time.sleep(5 * (attempt + 1))
            continue
        if r.status_code == 429 or r.status_code >= 500:
            wait = 30 * (attempt + 1)
            print(f"   t.me просит притормозить, поток ждёт {wait} с", flush=True)
            time.sleep(wait)
            continue
        return r.status_code, r.url, r.text
    return None


# ------------------------- разбор ---------------------------

def parse_count(s):
    s = s.replace(" ", " ").strip()
    m = re.match(r"([\d.,]+)\s*([KM])", s.replace(" ", ""), re.I)
    if m:
        return int(float(m.group(1).replace(",", ".")) * (1000 if m.group(2).upper() == "K" else 1_000_000))
    d = re.sub(r"[^\d]", "", s)
    return int(d) if d else 0


def text_with_links(el):
    if el is None:
        return ""
    return el.get_text(" ", strip=True) + " " + " ".join(a.get("href", "") for a in el.find_all("a"))


def parse_preview(soup):
    """Страница t.me/s/name — канал с открытым просмотром постов."""
    title = soup.select_one(".tgme_channel_info_header_title")
    desc = soup.select_one(".tgme_channel_info_description")
    subs = 0
    for c in soup.select(".tgme_channel_info_counter"):
        v, t = c.select_one(".counter_value"), c.select_one(".counter_type")
        if v and t and t.get_text(strip=True).startswith("subscriber"):
            subs = parse_count(v.get_text())
    posts = []
    for msg in soup.select(".tgme_widget_message"):
        tm = msg.select_one("time[datetime]")
        if not tm:
            continue
        try:
            dt = datetime.fromisoformat(tm["datetime"])
        except ValueError:
            continue
        views = msg.select_one(".tgme_widget_message_views")
        posts.append({"date": dt, "views": parse_count(views.get_text()) if views else 0,
                      "text": text_with_links(msg.select_one(".tgme_widget_message_text"))})
    return {"kind": "channel", "title": title.get_text(" ", strip=True) if title else "",
            "about": text_with_links(desc), "members": subs, "online": 0, "posts": posts}


def parse_main(soup):
    """Страница t.me/name — чат, канал со скрытыми постами, бот или пусто."""
    title = soup.select_one(".tgme_page_title")
    extra = soup.select_one(".tgme_page_extra")
    desc = soup.select_one(".tgme_page_description")
    info = {"kind": "none", "title": title.get_text(" ", strip=True) if title else "",
            "about": text_with_links(desc), "members": 0, "online": 0, "posts": []}
    ex = extra.get_text(" ", strip=True) if extra else ""
    for n, w in COUNT_RE.findall(ex):
        if w.startswith("subscriber"):
            info["members"], info["kind"] = parse_count(n), "channel"
        elif w.startswith("member"):
            info["members"], info["kind"] = parse_count(n), "chat"
        elif w == "online":
            info["online"] = parse_count(n)
    if info["kind"] == "none" and ex.startswith("@") and title:
        info["kind"] = "bot/user"
    return info


def load(name):
    if name.startswith(("+", "joinchat/")):
        res = fetch(f"https://t.me/{name}")
        if res is None:
            return None
        return parse_main(BeautifulSoup(res[2], "html.parser"))
    res = fetch(f"https://t.me/s/{name}")
    if res is None:
        return None
    status, url, html = res
    soup = BeautifulSoup(html, "html.parser")
    if "/s/" in url and soup.select_one(".tgme_channel_info"):
        return parse_preview(soup)
    return parse_main(soup)   # t.me/s/ перекинул на обычную страницу


# ------------------------- решение --------------------------

def decide(cat, name, info):
    now = datetime.now(timezone.utc)
    theme, _ = cat.split("_")
    row = {"категория": cat, "ссылка": f"https://t.me/{name}", "название": info["title"],
           "тип": info["kind"], "участники/подписчики": info["members"], "онлайн": info["online"],
           "дней без постов": "", "постов на странице": len(info["posts"]), "ER %": "",
           "TON очки": "", "TG-игра очки": "", "итог": "", "причина": ""}

    if info["kind"] == "none":
        row.update(итог="dead", причина="не существует или удалён")
        return row
    if info["kind"] == "bot/user":
        row.update(итог="dead", причина="это бот или человек")
        return row

    strong = f"{info['title']} {info['about']}"
    texts = [p["text"] for p in info["posts"] if p["text"].strip()]
    ton, ton_hits, _ = topic_score(TON_RE, strong, texts)
    tg, _, _ = topic_score(GAME_RE, strong, texts)
    row.update({"TON очки": ton, "TG-игра очки": tg})
    is_ton = ton >= TON_MIN_SCORE
    topic_ok = is_ton if theme == "crypto" else (is_ton or tg >= 2)

    if info["kind"] == "chat":
        if info["members"] < CHAT_MIN_MEMBERS:
            row.update(итог="dead", причина=f"мало участников ({info['members']})")
        elif USE_ONLINE and info["online"] < CHAT_MIN_ONLINE and info["members"] >= 300:
            row.update(итог="dead", причина=f"почти никого онлайн ({info['online']})")
        elif topic_ok:
            row.update(итог="ton", причина=f"в названии/описании: {pretty_hits(ton_hits)}")
        else:
            # сообщения чатов без аккаунта не видны — решит ступень 2
            row.update(итог="maybe", причина="чат живой, тему проверит ступень 2")
        return row

    # канал
    posts = info["posts"]
    if not posts:
        if topic_ok:
            row.update(итог="ton", причина="посты скрыты, TON в описании")
        else:
            row.update(итог="maybe", причина="посты скрыты, тему проверит ступень 2")
        return row
    days = (now - max(p["date"] for p in posts)).days
    views = sorted(p["views"] for p in posts if p["views"] and (now - p["date"]).total_seconds() > 86400)
    med = views[len(views) // 2] if views else 0
    er = med / info["members"] * 100 if info["members"] else 0
    row.update({"дней без постов": days, "ER %": round(er, 2)})
    if days > DEAD_DAYS:
        row.update(итог="dead", причина=f"не постит {days} дн.")
    elif topic_ok:
        row.update(итог="ton", причина=f"TON: {pretty_hits(ton_hits)}")
    else:
        row.update(итог="not_ton", причина="посты не про TON")
    return row


# ------------------------- main -----------------------------

def load_cache():
    done = {}
    if CACHE.exists():
        for line in CACHE.read_text(encoding="utf-8").splitlines():
            try:
                r = json.loads(line)
                done[r["_key"]] = r
            except (ValueError, KeyError):
                pass
    return done


def work(cat, name, key):
    time.sleep(random.uniform(*PAUSE))
    info = load(name)
    if info is None:
        row = {"категория": cat, "ссылка": f"https://t.me/{name}", "итог": "error",
               "причина": "t.me не ответил"}
    else:
        row = decide(cat, name, info)
    row["_key"] = key
    return row


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("files", nargs="*", default=["input.txt"])
    ap.add_argument("--as", dest="as_cat", choices=CATEGORIES,
                    help="категория для файлов без заголовков [Крипто чаты] и т.п.")
    ap.add_argument("--retry-errors", action="store_true", help="перепроверить ссылки с ошибкой сети")
    ap.add_argument("--no-online", action="store_true",
                    help="не выкидывать чаты за низкий онлайн (если запускаешь утром или ночью)")
    args = ap.parse_args()
    global USE_ONLINE
    USE_ONLINE = not args.no_online

    items, seen = [], set()
    for f in args.files:
        for cat, name in read_links(BASE / f if not Path(f).is_absolute() else f, args.as_cat):
            if name.lower() not in seen:
                seen.add(name.lower())
                items.append((cat, name))
    if not items:
        sys.exit("Ссылок не найдено. Если в файле нет заголовков, укажи категорию: --as crypto_channel")

    OUT.mkdir(exist_ok=True)
    done = load_cache()
    if args.retry_errors:
        done = {k: v for k, v in done.items() if v.get("итог") != "error"}
    todo = [(c, n, f"{c}:{n.lower()}") for c, n in items if f"{c}:{n.lower()}" not in done]
    print(f"Ссылок: {len(items)}, уже проверено: {len(items) - len(todo)}, осталось: {len(todo)}"
          + (" (через прокси)" if proxy_url() else " (БЕЗ прокси)"), flush=True)

    started, counts = time.time(), {}
    with ThreadPoolExecutor(WORKERS) as pool, open(CACHE, "a", encoding="utf-8") as cache_f:
        futures = [pool.submit(work, c, n, k) for c, n, k in todo]
        for i, fut in enumerate(as_completed(futures), 1):
            try:
                row = fut.result()
            except Exception as e:  # noqa: BLE001
                print(f"   ошибка: {e}", flush=True)
                continue
            done[row["_key"]] = row
            counts[row["итог"]] = counts.get(row["итог"], 0) + 1
            with _cache_lock:
                cache_f.write(json.dumps(row, ensure_ascii=False) + "\n")
                cache_f.flush()
            if i % 25 == 0 or i == len(todo):
                speed = i / max(1, time.time() - started)
                left_min = (len(todo) - i) / max(speed, 1e-6) / 60
                print(f"[{i}/{len(todo)}] {counts}  ~{left_min:.0f} мин осталось", flush=True)

    write_results([done[f"{c}:{n.lower()}"] for c, n in items if f"{c}:{n.lower()}" in done])


NAMES = {"crypto_chat": "Крипто чаты", "crypto_channel": "Крипто каналы",
         "game_chat": "Игровые чаты", "game_channel": "Игровые каналы"}


def write_results(rows):
    OUT.mkdir(exist_ok=True)
    fields = ["категория", "ссылка", "название", "тип", "участники/подписчики", "онлайн",
              "дней без постов", "постов на странице", "ER %", "TON очки", "TG-игра очки",
              "итог", "причина"]
    with open(OUT / "report.csv", "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=fields, delimiter=";", extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)

    lines = ["# Кандидаты на ступень 2. Сначала идут точно TON, потом «может быть».", ""]
    for cat, title in NAMES.items():
        part = [r for r in rows if r["категория"] == cat and r["итог"] in ("ton", "maybe")]
        if not part:
            continue
        part.sort(key=lambda r: (r["итог"] != "ton", -int(r.get("участники/подписчики") or 0)))
        lines.append(f"[{title}]")
        lines += [f"{r['ссылка']}   # {r['итог']}" for r in part]
        lines.append("")
    (OUT / "candidates.txt").write_text("\n".join(lines), encoding="utf-8")

    print("\nИТОГ СТУПЕНИ 1")
    for cat, title in NAMES.items():
        part = [r for r in rows if r["категория"] == cat]
        if not part:
            continue
        c = {k: sum(1 for r in part if r["итог"] == k) for k in ("ton", "maybe", "not_ton", "dead", "error")}
        print(f"  {title}: всего {len(part)} → TON {c['ton']}, может быть {c['maybe']}, "
              f"не TON {c['not_ton']}, мёртвые {c['dead']}, ошибки {c['error']}")
    print("\nКандидаты: stage1/candidates.txt   Таблица: stage1/report.csv")
    if any(r["итог"] == "error" for r in rows):
        print("Есть ошибки сети — перезапусти с флагом --retry-errors")


if __name__ == "__main__":
    main()
