"""Общие части для prefilter.py и tgfilter.py: ключевые слова TON,
чтение списков ссылок, настройки и подсчёт тематики."""
import configparser
import math
import re
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent
CONFIG = BASE / "config.ini"

# Признаки TON: (регулярка, вес)
TON_PATTERNS = [
    # --- сам TON (английский и русский: тон, ТОН, тона, тоне, тонкоин...) ---
    (r"\b(?:TON|Ton)\b", 2), (r"(?i)#ton\b", 2), (r"(?i)\btoncoin\b", 3), (r"(?i)\bthe open network\b", 3),
    (r"(?i)(?<![а-яё])тон(?:а|е|у|ом|ах)?(?![а-яё])", 2), (r"(?i)тонкоин|тонкойн", 3),
    (r"(?i)\bton\s?space\b", 3), (r"(?i)telegram wallet|телеграм[\s-]?кошел", 2),
    # --- Telegram и Дуров ---
    (r"(?i)\bdurov\b|дуров", 2),
    # --- Telegram-подарки и NFT ---
    (r"(?i)(?:tg|тг|telegram|телеграм\w*)[\s-]*(?:подар|gift)", 3),
    (r"(?i)nft[\s-]*подар|подар\w*\s+nft|коллекционн\w+\s+подар|улучш\w+\s+подар"
     r"|upgraded gifts?|collectible gifts?", 3),
    (r"(?i)(?<![а-яё])подар(?:ок|ки|ков|ками|кам|ках)(?![а-яё])|\bgifts?\b", 1),
    (r"(?i)\bnfts?\b|(?<![а-яё])нфт(?![а-яё])", 1),
    (r"(?i)@portals\b|portals[\s-]?market|\btonnel\b|\bMRKT\b|\bxgift\b", 3),
    (r"(?i)fragment\.com", 3), (r"(?i)анонимн\w+\s+номер|\+888", 2),
    # --- кошельки, DEX, проекты экосистемы ---
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


CATEGORIES = ("crypto_chat", "crypto_channel", "game_chat", "game_channel")


def read_links(path, default_cat=None):
    """Ссылки из файла. Раздел задаётся заголовком [Крипто чаты] и т.п.
    Если заголовков нет — всем ссылкам даётся default_cat."""
    path = Path(path)
    if not path.exists():
        sys.exit(f"Нет файла {path}")
    items, seen, cat = [], set(), default_cat
    for line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
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
    proxy = proxy_dict(cfg)
    return api_id, api_hash, proxy


def read_cfg():
    cfg = configparser.ConfigParser()
    if CONFIG.exists():
        cfg.read(CONFIG, encoding="utf-8")
    return cfg


def proxy_url():
    """Прокси в виде socks5h://user:pass@host:port для обычных веб-запросов."""
    cfg = read_cfg()
    if not (cfg.has_section("proxy") and cfg["proxy"].get("host", "").strip()):
        return ""
    p = cfg["proxy"]
    auth = ""
    if p.get("username", "").strip():
        auth = f"{p['username'].strip()}:{p.get('password', '').strip()}@"
    kind = p.get("type", "socks5").strip().lower()
    scheme = "socks5h" if kind.startswith("socks5") else kind
    return f"{scheme}://{auth}{p['host'].strip()}:{int(p['port'])}"


def proxy_dict(cfg):
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
    return proxy


# ------------------------- анализ текста --------------------

def topic_score(compiled, strong_text: str, texts):
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
