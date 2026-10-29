"""
Фильтр Telegram-чатов и каналов через Telegram API (Telethon).

Читает реальные сообщения и считает:
  чаты   — когда было последнее сообщение, сколько сообщений за сутки/неделю,
           сколько РАЗНЫХ людей пишут, есть ли там TON-тематика;
  каналы — частоту постов, ER, реакции и комментарии, TON-тематику,
           признаки игры в Telegram.

В чаты НЕ вступает и ничего не пишет — только читает публичное.

Запуск на VPS:
  python3 tgfilter.py            — проверить список из input.txt
  python3 tgfilter.py --fresh    — проверить всё заново, игнорируя сохранённые результаты
Первый запуск попросит номер телефона и код из Telegram (один раз).
"""
from __future__ import annotations

import argparse
import asyncio
import configparser
import csv
import json
import math
import random
import re
import statistics
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from telethon import TelegramClient, errors, functions, types

# ============================================================
#                         НАСТРОЙКИ
# Меняй цифры тут, если фильтр слишком строгий или мягкий.
# ============================================================

# --- Чаты (группы) ---
CHAT_MIN_MEMBERS = 50          # меньше участников — пустой чат
CHAT_MAX_DAYS_SILENT = 2       # последнее сообщение не старше N дней
CHAT_MIN_MSGS_7D = 50          # минимум сообщений от людей за 7 дней
CHAT_MIN_AUTHORS_7D = 10       # минимум РАЗНЫХ людей, писавших за 7 дней
CHAT_FETCH_LIMIT = 1500        # сколько последних сообщений максимум читать в чате

# --- Каналы ---
CHANNEL_MAX_DAYS_SILENT = 14   # последний пост не старше N дней
CHANNEL_MIN_POSTS_30D = 4      # минимум постов за 30 дней
CHANNEL_MIN_ER = 3.0           # медиана просмотров / подписчики, %
CHANNEL_FETCH_LIMIT = 40       # сколько последних постов читать

# --- Крипто-каналы ---
CRYPTO_CHANNEL_MAX_SUBS = 300_000   # «2 млн подписчиков» отсекаем

# --- Игровые каналы: игра должна жить в Telegram ---
GAME_CHANNEL_MAX_DAYS_SILENT = 45
TG_GAME_MIN_SCORE = 2

# --- Связь с TON ---
TON_MIN_SCORE = 3

# --- Бережём аккаунт от ограничений Telegram ---
DELAY_MIN, DELAY_MAX = 3.0, 6.0     # случайная пауза между проверками, сек
MAX_FLOOD_WAIT = 15 * 60            # если Telegram просит ждать дольше — сохраняемся и выходим

# ============================================================

BASE = Path(__file__).resolve().parent
INPUT = BASE / "input.txt"
CONFIG = BASE / "config.ini"
CACHE = BASE / "cache.json"
OUT = BASE / "results"
SESSION = str(BASE / "tgfilter")

# Признаки TON: (регулярка, вес)
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
    (r"(?i)\bTMA\b", 1), (r"(?i)telegram stars|звёзд|звезд", 1),
]

# Признаки «игра живёт в Telegram»
TG_GAME_PATTERNS = [
    (r"(?i)t\.me/\w+/\w+\?startapp", 3),
    (r"(?i)t\.me/\w+/\w+", 1),
    (r"(?i)[?&]startapp", 2),
    (r"(?i)t\.me/\w*bot\b", 2),
    (r"(?i)mini[\s-]?apps?\b", 2), (r"(?i)мини[\s-]?апп?", 2), (r"(?i)web\s?app", 1),
    (r"(?i)play (?:in|on) telegram|играй(?:те)? (?:в|прямо в) телеграм", 3),
    (r"(?i)играть в (?:telegram|телеграм)", 3),
    (r"(?i)\btap[\s-]?to[\s-]?earn\b|тапалк", 2), (r"(?i)\bclicker\b|кликер", 1),
    (r"(?i)\bTMA\b", 2), (r"\bTON\b", 1),
]

TON_RE = [(re.compile(p), w, p) for p, w in TON_PATTERNS]
GAME_RE = [(re.compile(p), w, p) for p, w in TG_GAME_PATTERNS]


# ------------------------- ввод -----------------------------

LINK_RE = re.compile(
    r"(?:https?://)?(?:t\.me|telegram\.me)/(?:s/)?(\+[\w-]+|joinchat/[\w-]+|[A-Za-z]\w{3,})"
    r"|(?<![\w.])@([A-Za-z]\w{3,})"
)


def category_of(header: str):
    h = header.lower()
    theme = "game" if ("игр" in h or "game" in h) else "crypto" if ("крипт" in h or "crypto" in h or "тон" in h) else None
    kind = "chat" if ("чат" in h or "chat" in h) else "channel" if ("канал" in h or "channel" in h) else None
    return f"{theme}_{kind}" if theme and kind else None


def read_input():
    if not INPUT.exists():
        sys.exit("Нет файла input.txt рядом с программой.")
    items, seen, cat = [], set(), None
    for line in INPUT.read_text(encoding="utf-8").splitlines():
        s = line.strip()
        if not s:
            continue
        if (s.startswith("[") and s.endswith("]")) or s.startswith("#"):
            c = category_of(s.strip("[]# "))
            if c:
                cat = c
            continue
        if cat is None:
            continue
        for m in LINK_RE.finditer(s):
            name = m.group(1) or m.group(2)
            if name.lower() in seen:
                continue
            seen.add(name.lower())
            items.append((cat, name))
    return items


def load_config():
    if not CONFIG.exists():
        sys.exit("Нет config.ini. Скопируй config.example.ini в config.ini и впиши api_id и api_hash.")
    cfg = configparser.ConfigParser()
    cfg.read(CONFIG, encoding="utf-8")
    try:
        api_id = int(cfg["telegram"]["api_id"])
        api_hash = cfg["telegram"]["api_hash"].strip()
    except (KeyError, ValueError):
        sys.exit("В config.ini не заполнены api_id / api_hash.")
    proxy = None
    if cfg.has_section("proxy") and cfg["proxy"].get("host", "").strip():
        p = cfg["proxy"]
        proxy = {
            "proxy_type": p.get("type", "socks5").strip(),
            "addr": p["host"].strip(),
            "port": int(p["port"]),
            "username": p.get("username", "").strip() or None,
            "password": p.get("password", "").strip() or None,
            "rdns": True,
        }
    return api_id, api_hash, proxy


# ------------------------- анализ текста --------------------

def topic_score(compiled, strong_text: str, texts: list[str]):
    """Очки темы. Совпадение в названии/описании — вес x3.
    В сообщениях считается, только если тема встречается регулярно,
    а не в одном случайном сообщении."""
    need = max(2, math.ceil(len(texts) * 0.03))
    total, hits = 0, []
    for rx, w, raw in compiled:
        if rx.search(strong_text):
            total += w * 3
            hits.append(raw)
        elif texts and sum(1 for t in texts if rx.search(t)) >= need:
            total += w
            hits.append(raw)
    share = 0.0
    if texts:
        share = sum(1 for t in texts if any(rx.search(t) for rx, _, _ in compiled)) / len(texts) * 100
    return total, hits, share


def pretty_hits(hits):
    out = []
    for h in hits[:6]:
        h = re.sub(r"\(\?i\)|\\b|\(\?<!\[а-яё\]\)|\(\?!\[а-яё\]\)|\\|\?:|[()^$]", "", h)
        out.append(h.split("|")[0][:18])
    return ", ".join(out)


# ------------------------- оценка ---------------------------

def empty_row(cat, name):
    return {
        "категория": cat, "ссылка": f"https://t.me/{name}", "название": "", "тип": "",
        "участники/подписчики": "", "онлайн": "",
        "дней с последнего сообщения": "", "сообщений за 24ч": "", "сообщений за 7д": "",
        "авторов за 7д": "", "топ-автор %": "",
        "постов за 30д": "", "медиана просмотров": "", "ER %": "",
        "реакций на пост": "", "комментов на пост": "",
        "TON очки": "", "TON % сообщений": "", "TG-игра очки": "",
        "решение": "", "причина": "", "предупреждение": "",
    }


def evaluate(cat: str, name: str, info: dict, msgs: list[dict], now: datetime) -> dict:
    """info: title, about, kind ('chat'/'channel'), members, online.
    msgs: date, views, reactions, comments, sender_id, is_bot, text (от новых к старым)."""
    theme, listed_as = cat.split("_")
    row = empty_row(cat, name)
    kind = info["kind"]
    row.update({"название": info["title"], "тип": "чат" if kind == "chat" else "канал",
                "участники/подписчики": info["members"], "онлайн": info.get("online") or ""})

    strong = f"{info['title']} {info['about']}"
    texts = [m["text"] for m in msgs if m["text"].strip()]
    ton, ton_hits, ton_share = topic_score(TON_RE, strong, texts)
    tg, tg_hits, _ = topic_score(GAME_RE, strong, texts)
    row.update({"TON очки": ton, "TON % сообщений": round(ton_share, 1), "TG-игра очки": tg})

    reasons, warns = [], []

    if kind == "chat":
        week = [m for m in msgs if now - m["date"] <= timedelta(days=7) and not m["is_bot"]]
        day = [m for m in week if now - m["date"] <= timedelta(days=1)]
        authors = {}
        for m in week:
            authors[m["sender_id"]] = authors.get(m["sender_id"], 0) + 1
        top_pct = max(authors.values()) / len(week) * 100 if week else 0
        last_days = (now - msgs[0]["date"]).days if msgs else None
        row.update({"дней с последнего сообщения": "" if last_days is None else last_days,
                    "сообщений за 24ч": len(day), "сообщений за 7д": len(week),
                    "авторов за 7д": len(authors), "топ-автор %": round(top_pct)})

        if info["members"] < CHAT_MIN_MEMBERS:
            reasons.append(f"мало участников ({info['members']})")
        if last_days is None:
            reasons.append("сообщений нет или их не видно")
        elif last_days > CHAT_MAX_DAYS_SILENT:
            reasons.append(f"молчит {last_days} дн.")
        if len(week) < CHAT_MIN_MSGS_7D:
            reasons.append(f"мало сообщений за неделю ({len(week)})")
        if len(authors) < CHAT_MIN_AUTHORS_7D:
            reasons.append(f"пишут всего {len(authors)} чел.")
        if week and top_pct > 60:
            warns.append(f"один человек пишет {top_pct:.0f}% сообщений")
        if len(day) >= CHAT_FETCH_LIMIT * 0.9:
            warns.append("очень много сообщений — возможно флуд/спам")
        if theme == "crypto" and ton < TON_MIN_SCORE:
            reasons.append("нет связи с TON")
        if listed_as == "channel":
            warns.append("в списке как канал, а это чат")

    else:
        subs = info["members"]
        if msgs:
            last_days = (now - msgs[0]["date"]).days
            posts_30 = sum(1 for m in msgs if now - m["date"] <= timedelta(days=30))
            mature = [m for m in msgs if now - m["date"] > timedelta(hours=24)]
            views = [m["views"] for m in mature if m["views"]]
            med_views = int(statistics.median(views)) if views else 0
            er = med_views / subs * 100 if subs else 0
            reacts = statistics.mean([m["reactions"] for m in mature]) if mature else 0
            comms = statistics.mean([m["comments"] for m in mature]) if mature else 0
            row.update({"дней с последнего сообщения": last_days, "постов за 30д": posts_30,
                        "медиана просмотров": med_views, "ER %": round(er, 1),
                        "реакций на пост": round(reacts, 1), "комментов на пост": round(comms, 1)})
            # просмотры есть, а людей нет — похоже на накрутку просмотров
            if med_views >= 1000 and reacts < med_views * 0.001 and comms < 1:
                warns.append("много просмотров, но почти нет реакций/комментов — возможна накрутка")
        else:
            last_days, posts_30, er = None, 0, 0

        if theme == "game":
            if last_days is None:
                warns.append("постов не видно")
            elif last_days > GAME_CHANNEL_MAX_DAYS_SILENT:
                reasons.append(f"заброшен ({last_days} дн. без постов)")
            if tg < TG_GAME_MIN_SCORE:
                reasons.append("не видно, что игра в Telegram")
        else:
            if last_days is None:
                reasons.append("постов нет")
            else:
                if last_days > CHANNEL_MAX_DAYS_SILENT:
                    reasons.append(f"не постит {last_days} дн.")
                if posts_30 < CHANNEL_MIN_POSTS_30D:
                    reasons.append(f"мало постов за месяц ({posts_30})")
                if er < CHANNEL_MIN_ER:
                    reasons.append(f"ER {er:.1f}% — мёртвая или накрученная аудитория")
            if subs > CRYPTO_CHANNEL_MAX_SUBS:
                reasons.append(f"слишком большой ({subs:,})".replace(",", " "))
            if ton < TON_MIN_SCORE:
                reasons.append("нет связи с TON")
        if listed_as == "chat":
            warns.append("в списке как чат, а это канал")

    if reasons:
        row.update(решение="шлак", причина="; ".join(reasons))
    else:
        why = []
        if theme == "crypto":
            why.append(f"TON: {pretty_hits(ton_hits)}")
        if theme == "game" and kind == "channel":
            why.append(f"TG-игра: {pretty_hits(tg_hits)}")
        row.update(решение="оставить", причина="; ".join(why) or "активный")
    row["предупреждение"] = "; ".join(warns)
    return row


# ------------------------- Telegram -------------------------

def msg_to_dict(m) -> dict:
    parts = [m.message or ""]
    for e in (m.entities or []):
        if isinstance(e, types.MessageEntityTextUrl):
            parts.append(e.url)
    markup = getattr(m, "reply_markup", None)
    for row in getattr(markup, "rows", None) or []:
        for b in row.buttons:
            url = getattr(b, "url", None)
            if url:
                parts.append(url)
    reactions = 0
    if m.reactions and m.reactions.results:
        reactions = sum(r.count for r in m.reactions.results)
    sender = m.sender
    return {
        "date": m.date,
        "views": m.views or 0,
        "reactions": reactions,
        "comments": m.replies.replies if m.replies else 0,
        "sender_id": m.sender_id,
        "is_bot": bool(getattr(sender, "bot", False)),
        "text": " ".join(parts),
    }


async def fetch_messages(client, entity, kind: str, now: datetime):
    out = []
    limit = CHANNEL_FETCH_LIMIT if kind == "channel" else CHAT_FETCH_LIMIT
    async for m in client.iter_messages(entity, limit=limit):
        if isinstance(m, types.MessageService):
            continue
        if kind == "chat" and now - m.date > timedelta(days=7):
            if not out:              # запомним хотя бы последнее, чтобы знать, сколько чат молчит
                out.append(msg_to_dict(m))
            break
        out.append(msg_to_dict(m))
    return out


async def check(client, cat: str, name: str, now: datetime) -> dict:
    row = empty_row(cat, name)

    # приватная ссылка-приглашение
    if name.startswith("+") or name.startswith("joinchat/"):
        h = name[1:] if name.startswith("+") else name.split("/", 1)[1]
        inv = await client(functions.messages.CheckChatInviteRequest(h))
        if isinstance(inv, types.ChatInvite):
            row.update({"название": inv.title, "тип": "канал" if inv.broadcast else "чат",
                        "участники/подписчики": inv.participants_count,
                        "решение": "не проверено",
                        "причина": "приватный: без вступления сообщения не прочитать"})
            return row
        entity = inv.chat
    else:
        entity = await client.get_entity(name)

    if isinstance(entity, types.User):
        row.update(решение="шлак", причина="это бот или человек, не чат/канал",
                   название=" ".join(filter(None, [entity.first_name, entity.last_name])))
        return row

    if isinstance(entity, types.Channel):
        full = await client(functions.channels.GetFullChannelRequest(entity))
        fc = full.full_chat
        kind = "channel" if entity.broadcast and not entity.gigagroup else "chat"
        info = {"title": entity.title, "about": fc.about or "", "kind": kind,
                "members": fc.participants_count or 0, "online": getattr(fc, "online_count", None)}
    else:  # обычная маленькая группа
        full = await client(functions.messages.GetFullChatRequest(entity.id))
        info = {"title": entity.title, "about": full.full_chat.about or "", "kind": "chat",
                "members": getattr(entity, "participants_count", 0) or 0, "online": None}

    await asyncio.sleep(1)
    msgs = await fetch_messages(client, entity, info["kind"], now)
    return evaluate(cat, name, info, msgs, now)


# ------------------------- отчёты ---------------------------

NAMES = {
    "game_chat": "Игровые чаты",
    "game_channel": "Игровые каналы (игры в Telegram)",
    "crypto_channel": "Крипто-каналы (TON)",
    "crypto_chat": "Крипто-чаты (TON)",
}


def num(v):
    return v if isinstance(v, (int, float)) else 0


def write_reports(rows):
    OUT.mkdir(exist_ok=True)
    stamp = datetime.now().strftime("%Y-%m-%d_%H-%M")
    csv_path = OUT / f"report_{stamp}.csv"
    with open(csv_path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=list(empty_row("", "").keys()), delimiter=";")
        w.writeheader()
        w.writerows(rows)

    lines = [f"# Итог фильтрации — {stamp}", ""]
    for cat, title in NAMES.items():
        part = [r for r in rows if r["категория"] == cat]
        if not part:
            continue
        kept = [r for r in part if r["решение"] == "оставить"]
        if cat.endswith("chat"):
            kept.sort(key=lambda r: -num(r["сообщений за 7д"]))
        else:
            kept.sort(key=lambda r: -num(r["медиана просмотров"]))
        lines += [f"## {title}: оставлено {len(kept)} из {len(part)}", ""]
        for r in kept:
            if r["тип"] == "чат":
                stat = (f"{r['участники/подписчики']} уч., {r['сообщений за 7д']} сообщ./нед., "
                        f"{r['авторов за 7д']} авторов")
            else:
                stat = f"{r['участники/подписчики']} подп., ER {r['ER %']}%, {r['постов за 30д']} постов/мес"
            warn = f" ⚠ {r['предупреждение']}" if r["предупреждение"] else ""
            lines.append(f"- {r['название']} — {r['ссылка']} ({stat}){warn}")
        lines.append("")
    unchecked = [r for r in rows if r["решение"] == "не проверено"]
    if unchecked:
        lines += ["## Не удалось проверить", ""]
        lines += [f"- {r['ссылка']} — {r['причина']}" for r in unchecked]
    md_path = OUT / f"summary_{stamp}.md"
    md_path.write_text("\n".join(lines), encoding="utf-8")
    return csv_path, md_path, "\n".join(lines)


# ------------------------- main -----------------------------

def load_cache():
    try:
        return json.loads(CACHE.read_text(encoding="utf-8"))
    except (FileNotFoundError, ValueError):
        return {}


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--fresh", action="store_true", help="проверить всё заново")
    args = ap.parse_args()

    items = read_input()
    if not items:
        sys.exit("В input.txt нет ссылок. Проверь заголовки разделов в [квадратных скобках].")
    api_id, api_hash, proxy = load_config()
    cache = {} if args.fresh else load_cache()

    client = TelegramClient(SESSION, api_id, api_hash, proxy=proxy)
    await client.start()   # первый раз спросит телефон и код
    me = await client.get_me()
    print(f"Вошёл как {me.first_name}. Ссылок: {len(items)}, уже проверено ранее: "
          f"{sum(1 for c, n in items if f'{c}:{n.lower()}' in cache)}")

    stopped = False
    for i, (cat, name) in enumerate(items, 1):
        key = f"{cat}:{name.lower()}"
        if key in cache:
            continue
        print(f"[{i}/{len(items)}] {name} ...", end=" ", flush=True)
        now = datetime.now(timezone.utc)
        row = None
        for _ in range(3):
            try:
                row = await check(client, cat, name, now)
                break
            except errors.FloodWaitError as e:
                if e.seconds > MAX_FLOOD_WAIT:
                    print(f"\nTelegram просит паузу {e.seconds // 60} мин. Прогресс сохранён — "
                          f"запусти позже, продолжу с этого места.")
                    stopped = True
                    break
                print(f"(пауза {e.seconds} с по просьбе Telegram)", end=" ", flush=True)
                await asyncio.sleep(e.seconds + 5)
            except (errors.UsernameNotOccupiedError, errors.UsernameInvalidError, ValueError):
                row = empty_row(cat, name)
                row.update(решение="шлак", причина="не существует")
                break
            except (errors.ChannelPrivateError, errors.InviteHashExpiredError,
                    errors.InviteHashInvalidError, errors.ChannelBannedError):
                row = empty_row(cat, name)
                row.update(решение="шлак", причина="закрыт, удалён или ссылка недействительна")
                break
            except Exception as e:  # noqa: BLE001 — один плохой чат не должен ронять всю проверку
                row = empty_row(cat, name)
                row.update(решение="не проверено", причина=f"ошибка: {e.__class__.__name__}")
                break
        if stopped:
            break
        if row is None:
            row = empty_row(cat, name)
            row.update(решение="не проверено", причина="Telegram не дал проверить")
        print(f"{row['решение'].upper()} — {row['причина']}"
              + (f"  ⚠ {row['предупреждение']}" if row["предупреждение"] else ""))
        cache[key] = row
        CACHE.write_text(json.dumps(cache, ensure_ascii=False, indent=1), encoding="utf-8")
        await asyncio.sleep(random.uniform(DELAY_MIN, DELAY_MAX))

    await client.disconnect()

    rows = [cache[f"{c}:{n.lower()}"] for c, n in items if f"{c}:{n.lower()}" in cache]
    if not rows:
        return
    csv_path, md_path, summary = write_reports(rows)
    print("\n" + summary)
    left = len(items) - len(rows)
    print(f"\nОтчёты: {md_path.name} и {csv_path.name} в папке results/"
          + (f"\nОсталось проверить: {left} — запусти программу ещё раз позже." if left else ""))


if __name__ == "__main__":
    asyncio.run(main())
