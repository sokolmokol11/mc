"""
Фильтр Telegram-чатов и каналов.

Читает input.txt (ссылки по разделам), открывает публичные страницы t.me
(без входа в аккаунт, без API-ключей), собирает статистику и решает,
что оставить, а что выкинуть. Результаты кладёт в папку results/.

Запуск:  python filter.py
"""
from __future__ import annotations

import csv
import os
import re
import statistics
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import requests
from bs4 import BeautifulSoup

# ============================================================
#                       НАСТРОЙКИ
# Меняй цифры тут, если фильтр слишком строгий или мягкий.
# ============================================================

# Прокси для VPS, где Telegram не открывается напрямую.
# Пример: socks5h://user:pass@1.2.3.4:1080   (можно задать через переменную TG_PROXY)
PROXY = os.environ.get("TG_PROXY", "")

DELAY = 1.5                 # пауза между запросами, сек (не ставь меньше 1)

# --- Чаты (группы): активность по числу людей онлайн ---
CHAT_MIN_MEMBERS = 50       # меньше участников — мёртвый/пустой чат
CHAT_MIN_ONLINE = 10        # минимум людей онлайн прямо сейчас
CHAT_MIN_ONLINE_PCT = 0.3   # минимум % онлайн от участников (накрутка = большой чат, 0 онлайн)

# --- Каналы: активность и «живость» аудитории ---
CHANNEL_MAX_DAYS_SILENT = 14    # последний пост не старше N дней
CHANNEL_MIN_POSTS_30D = 4       # минимум постов за 30 дней (из видимых на странице)
CHANNEL_MIN_ER = 3.0            # средние просмотры / подписчики, % (ниже — накрутка или мёртвая аудитория)

# --- Крипто-каналы: дополнительно ---
CRYPTO_CHANNEL_MAX_SUBS = 300_000   # «2 млн подписчиков» отсекаем

# --- Игровые каналы: игра должна жить в Telegram ---
GAME_CHANNEL_MAX_DAYS_SILENT = 45   # тут мягче: достаточно, что канал не заброшен
TG_GAME_MIN_SCORE = 2               # сколько признаков «игра в Telegram» нужно

# --- Связь с TON ---
TON_MIN_SCORE = 3                   # сколько очков TON-тематики нужно крипто-чатам/каналам

# ============================================================

BASE = Path(__file__).resolve().parent
INPUT = BASE / "input.txt"
OUT = BASE / "results"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/124.0 Safari/537.36",
    "Accept-Language": "en-US,en;q=0.9",
}

# Признаки TON: (регулярка, вес). В названии/описании вес утраивается.
TON_PATTERNS = [
    (r"\bTON\b", 2), (r"(?i)#ton\b", 2), (r"(?i)\btoncoin\b", 3), (r"(?i)\bthe open network\b", 3),
    (r"(?i)(?<![а-яё])тон(?:а|е|у|ом)?(?![а-яё])", 2), (r"(?i)тонкоин", 3),
    (r"(?i)\btonkeeper\b", 3), (r"(?i)\bmytonwallet\b", 3), (r"(?i)\btonhub\b", 3),
    (r"(?i)\btonviewer\b", 2), (r"(?i)\btonscan\b", 2), (r"(?i)\btonapi\b", 2),
    (r"(?i)\bjettons?\b", 3), (r"(?i)жетон", 2),
    (r"(?i)ston\.?fi", 3), (r"(?i)\bdedust\b", 3), (r"(?i)\bgetgems\b", 3), (r"(?i)\bfragment\b", 1),
    (r"(?i)\bevaa\b", 2), (r"(?i)storm\s?trade", 2), (r"(?i)\btonco\b", 2), (r"(?i)\btonstakers\b", 3),
    (r"(?i)\bnotcoin\b", 2), (r"\$NOT\b", 2), (r"(?i)\bdogs\b", 1), (r"(?i)hamster kombat", 1),
    (r"(?i)\bcatizen\b", 1), (r"(?i)\bblum\b", 1), (r"(?i)\btapps?\b", 1),
    (r"(?i)@wallet\b", 2), (r"(?i)mini[\s-]?apps?\b", 1), (r"(?i)мини[\s-]?апп?", 1),
    (r"(?i)\bTMA\b", 1), (r"(?i)telegram stars|\bstars\b|звёзд|звезд", 1),
]

# Признаки «игра живёт в Telegram»
TG_GAME_PATTERNS = [
    (r"(?i)t\.me/[\w]+/[\w]+\?startapp", 3),    # прямая ссылка на mini app
    (r"(?i)t\.me/[\w]+/[\w]+", 1),              # t.me/bot/app
    (r"(?i)[?&]startapp", 2),
    (r"(?i)t\.me/[\w]*bot\b", 2),               # ссылка на игрового бота
    (r"(?i)mini[\s-]?apps?\b", 2), (r"(?i)мини[\s-]?апп?", 2), (r"(?i)web\s?app", 1),
    (r"(?i)play (?:in|on) telegram|играй(?:те)? (?:в|прямо в) телеграм", 3),
    (r"(?i)играть в (?:telegram|телеграм)", 3),
    (r"(?i)\btap[\s-]?to[\s-]?earn\b|тапалк", 2), (r"(?i)\bclicker\b|кликер", 1),
    (r"(?i)\bTMA\b", 2), (r"\bTON\b", 1),
]


# ------------------------- ввод -----------------------------

LINK_RE = re.compile(
    r"(?:https?://)?(?:t\.me|telegram\.me)/(?:s/)?(\+[\w-]+|joinchat/[\w-]+|[A-Za-z][\w]{3,})"
    r"|(?<![\w.])@([A-Za-z][\w]{3,})"
)


def category_of(header: str) -> str | None:
    h = header.lower()
    theme = "game" if "игр" in h or "game" in h else "crypto" if ("крипт" in h or "crypto" in h or "тон" in h) else None
    kind = "chat" if "чат" in h or "chat" in h else "channel" if "канал" in h or "channel" in h else None
    if theme and kind:
        return f"{theme}_{kind}"
    return None


def read_input() -> list[tuple[str, str]]:
    if not INPUT.exists():
        sys.exit(f"Нет файла {INPUT.name}. Создай его по образцу input_example.txt")
    items, seen, cat = [], set(), None
    for line in INPUT.read_text(encoding="utf-8").splitlines():
        s = line.strip()
        if not s:
            continue
        if s.startswith("[") and s.endswith("]") or s.startswith("#"):
            c = category_of(s.strip("[]# "))
            if c:
                cat = c
            continue
        if cat is None:
            continue
        for m in LINK_RE.finditer(s):
            name = m.group(1) or m.group(2)
            key = name.lower()
            if key in seen:
                continue
            seen.add(key)
            items.append((cat, name))
    return items


# ------------------------- сеть -----------------------------

session = requests.Session()
session.headers.update(HEADERS)
if PROXY:
    session.proxies = {"http": PROXY, "https": PROXY}


def get(url: str) -> requests.Response | None:
    for attempt in range(4):
        try:
            r = session.get(url, timeout=20)
            if r.status_code == 429:
                wait = 30 * (attempt + 1)
                print(f"   Telegram просит притормозить, жду {wait} с...")
                time.sleep(wait)
                continue
            return r
        except requests.RequestException as e:
            print(f"   ошибка сети ({e.__class__.__name__}), повтор...")
            time.sleep(5)
    return None


# ------------------------- разбор ---------------------------

def parse_count(s: str) -> int:
    s = s.replace("\u00a0", " ").strip()
    m = re.match(r"([\d.,]+)\s*([KM])", s.replace(" ", ""), re.I)
    if m:
        num = float(m.group(1).replace(",", "."))
        return int(num * (1000 if m.group(2).upper() == "K" else 1_000_000))
    digits = re.sub(r"[^\d]", "", s)
    return int(digits) if digits else 0


COUNT_RE = re.compile(r"(\d[\d\s\u00a0.,]*[KMkm]?)\s+(subscribers?|members?|online)")


def parse_main_page(html: str) -> dict:
    soup = BeautifulSoup(html, "html.parser")
    title_el = soup.select_one(".tgme_page_title")
    extra_el = soup.select_one(".tgme_page_extra")
    desc_el = soup.select_one(".tgme_page_description")
    info = {
        "title": title_el.get_text(" ", strip=True) if title_el else "",
        "description": desc_el.get_text(" ", strip=True) if desc_el else "",
        "type": "unknown", "subscribers": 0, "members": 0, "online": 0,
    }
    if desc_el:  # ссылки из описания тоже учитываем
        info["description"] += " " + " ".join(a.get("href", "") for a in desc_el.find_all("a"))
    extra = extra_el.get_text(" ", strip=True) if extra_el else ""
    for num, word in COUNT_RE.findall(extra):
        if word.startswith("subscriber"):
            info["subscribers"], info["type"] = parse_count(num), "channel"
        elif word.startswith("member"):
            info["members"], info["type"] = parse_count(num), "chat"
        elif word == "online":
            info["online"] = parse_count(num)
    if info["type"] == "unknown" and extra.startswith("@"):
        info["type"] = "bot/user"
    if not title_el or "If you have Telegram, you can contact" in info["description"] and not extra:
        info["type"] = "not_found" if info["type"] == "unknown" else info["type"]
    return info


def parse_posts(html: str) -> list[dict]:
    soup = BeautifulSoup(html, "html.parser")
    posts = []
    for msg in soup.select(".tgme_widget_message"):
        text_el = msg.select_one(".tgme_widget_message_text")
        views_el = msg.select_one(".tgme_widget_message_views")
        time_el = msg.select_one("time[datetime]")
        if not time_el:
            continue
        try:
            dt = datetime.fromisoformat(time_el["datetime"])
        except ValueError:
            continue
        links = " ".join(a.get("href", "") for a in msg.find_all("a"))
        posts.append({
            "date": dt,
            "views": parse_count(views_el.get_text()) if views_el else 0,
            "text": (text_el.get_text(" ", strip=True) if text_el else "") + " " + links,
        })
    return posts


def score(patterns, strong_text: str, weak_text: str) -> tuple[int, list[str]]:
    total, hits = 0, []
    for pat, w in patterns:
        if re.search(pat, strong_text):
            total += w * 3
            hits.append(pat)
        elif re.search(pat, weak_text):
            total += w
            hits.append(pat)
    return total, hits


def pretty_hits(hits: list[str]) -> str:
    out = []
    for h in hits[:6]:
        h = re.sub(r"\(\?i\)|\\b|\(\?<!\[а-яё\]\)|\(\?!\[а-яё\]\)|\\|\?:|[()^$]", "", h)
        out.append(h.split("|")[0][:18])
    return ", ".join(out)


# ------------------------- проверка -------------------------

def check(cat: str, name: str) -> dict:
    theme, kind_wanted = cat.split("_")
    row = {"категория": cat, "ссылка": f"https://t.me/{name}", "название": "", "тип": "",
           "подписчики/участники": "", "онлайн": "", "онлайн %": "", "дней без постов": "",
           "постов за 30д": "", "ср. просмотры": "", "ER %": "", "TON очки": "", "TG-игра очки": "",
           "решение": "", "причина": ""}

    r = get(f"https://t.me/{name}")
    if r is None or r.status_code != 200:
        row.update(решение="не проверено", причина="страница не открылась")
        return row
    info = parse_main_page(r.text)
    row["название"], row["тип"] = info["title"], info["type"]

    if info["type"] in ("not_found", "unknown"):
        row.update(решение="шлак", причина="не существует или закрыт")
        return row
    if info["type"] == "bot/user":
        row.update(решение="шлак", причина="это бот или человек, не чат/канал")
        return row

    posts = []
    if info["type"] == "channel" and not name.startswith(("+", "joinchat")):
        time.sleep(DELAY)
        rs = get(f"https://t.me/s/{name}")
        if rs is not None and rs.status_code == 200 and "/s/" in rs.url:
            posts = parse_posts(rs.text)

    strong = f"{info['title']} {info['description']}"
    weak = " ".join(p["text"] for p in posts)
    ton, ton_hits = score(TON_PATTERNS, strong, weak)
    tg, tg_hits = score(TG_GAME_PATTERNS, strong, weak)
    row["TON очки"], row["TG-игра очки"] = ton, tg

    reasons = []

    # ---- группы ----
    if info["type"] == "chat":
        m, on = info["members"], info["online"]
        pct = on / m * 100 if m else 0
        row.update({"подписчики/участники": m, "онлайн": on, "онлайн %": round(pct, 2)})
        if m < CHAT_MIN_MEMBERS:
            reasons.append(f"мало участников ({m})")
        if on < CHAT_MIN_ONLINE:
            reasons.append(f"мало онлайн ({on})")
        if m and pct < CHAT_MIN_ONLINE_PCT:
            reasons.append(f"онлайн {pct:.2f}% — похоже на мёртвый/накрученный")
        if theme == "crypto" and ton < TON_MIN_SCORE:
            reasons.append("нет связи с TON")
        if kind_wanted == "channel":
            reasons.append("(в списке как канал, а это чат)") if not reasons else None

    # ---- каналы ----
    else:
        subs = info["subscribers"]
        row["подписчики/участники"] = subs
        now = datetime.now(timezone.utc)
        if posts:
            last = max(p["date"] for p in posts)
            days_silent = (now - last).days
            posts_30 = sum(1 for p in posts if (now - p["date"]).days <= 30)
            # свежие посты (<24ч) ещё не набрали просмотры — не учитываем
            mature = [p["views"] for p in posts if (now - p["date"]).total_seconds() > 86400 and p["views"]]
            avg_views = int(statistics.median(mature)) if mature else 0
            er = avg_views / subs * 100 if subs else 0
            row.update({"дней без постов": days_silent, "постов за 30д": posts_30,
                        "ср. просмотры": avg_views, "ER %": round(er, 1)})
        else:
            days_silent, posts_30, er = None, 0, 0

        if theme == "game":
            if posts and days_silent > GAME_CHANNEL_MAX_DAYS_SILENT:
                reasons.append(f"заброшен ({days_silent} дн. без постов)")
            if tg < TG_GAME_MIN_SCORE:
                reasons.append("не видно, что игра в Telegram")
        else:
            if not posts:
                reasons.append("посты не видны, активность не проверить")
            else:
                if days_silent > CHANNEL_MAX_DAYS_SILENT:
                    reasons.append(f"не постит {days_silent} дн.")
                if posts_30 < CHANNEL_MIN_POSTS_30D:
                    reasons.append(f"мало постов за месяц ({posts_30})")
                if er < CHANNEL_MIN_ER:
                    reasons.append(f"ER {er:.1f}% — мёртвая/накрученная аудитория")
            if subs > CRYPTO_CHANNEL_MAX_SUBS:
                reasons.append(f"слишком большой ({subs:,})".replace(",", " "))
            if ton < TON_MIN_SCORE:
                reasons.append("нет связи с TON")
        if kind_wanted == "chat" and not reasons:
            reasons.append("(в списке как чат, а это канал)")

    real_reasons = [x for x in reasons if not x.startswith("(")]
    if real_reasons:
        row.update(решение="шлак", причина="; ".join(reasons))
    else:
        why = []
        if theme == "crypto":
            why.append(f"TON: {pretty_hits(ton_hits)}")
        if theme == "game" and info["type"] == "channel":
            why.append(f"TG-игра: {pretty_hits(tg_hits)}")
        why += reasons
        row.update(решение="оставить", причина="; ".join(why) or "активный")
    return row


# ------------------------- main -----------------------------

NAMES = {
    "game_chat": "Игровые чаты",
    "game_channel": "Игровые каналы (игры в Telegram)",
    "crypto_chat": "Крипто-чаты (TON)",
    "crypto_channel": "Крипто-каналы (TON)",
}


def main():
    items = read_input()
    if not items:
        sys.exit("В input.txt не нашлось ссылок. Проверь заголовки разделов в [квадратных скобках].")
    print(f"Ссылок к проверке: {len(items)}" + (" (через прокси)" if PROXY else ""))
    OUT.mkdir(exist_ok=True)

    rows = []
    for i, (cat, name) in enumerate(items, 1):
        print(f"[{i}/{len(items)}] {name} ...", end=" ", flush=True)
        row = check(cat, name)
        rows.append(row)
        print(f"{row['решение'].upper()} — {row['причина']}")
        time.sleep(DELAY)

    stamp = datetime.now().strftime("%Y-%m-%d_%H-%M")
    # Полный отчёт: открывается в Excel/Google Таблицах (разделитель ;)
    with open(OUT / f"report_{stamp}.csv", "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()), delimiter=";")
        w.writeheader()
        w.writerows(rows)

    # Короткий итог: только то, что осталось, по разделам
    lines = [f"# Итог фильтрации — {stamp}", ""]
    for cat, title in NAMES.items():
        kept = [r for r in rows if r["категория"] == cat and r["решение"] == "оставить"]
        total = sum(1 for r in rows if r["категория"] == cat)
        if not total:
            continue
        kept.sort(key=lambda r: -int(r["онлайн"] or r["ср. просмотры"] or 0))
        lines += [f"## {title}: оставлено {len(kept)} из {total}", ""]
        for r in kept:
            stat = (f"{r['подписчики/участники']} уч., {r['онлайн']} онлайн" if r["тип"] == "chat"
                    else f"{r['подписчики/участники']} подп., ER {r['ER %']}%")
            lines.append(f"- {r['название']} — {r['ссылка']} ({stat})")
        lines.append("")
    unchecked = [r for r in rows if r["решение"] == "не проверено"]
    if unchecked:
        lines += ["## Не удалось проверить", ""] + [f"- {r['ссылка']}" for r in unchecked]
    (OUT / f"summary_{stamp}.md").write_text("\n".join(lines), encoding="utf-8")

    kept_n = sum(1 for r in rows if r["решение"] == "оставить")
    print(f"\nГотово: оставлено {kept_n} из {len(rows)}. Смотри папку results/")


if __name__ == "__main__":
    main()
