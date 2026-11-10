"""Бэктест: загрузка истории с Bybit и «машина времени», которая отдаёт ботам рынок так, как он выглядел тогда.

Боты не знают, что торгуют в прошлом: BacktestClient подменяет биржу и показывает только то, что было известно
на момент симуляции (закрытые свечи, цена внутри текущей свечи, фандинг, открытый интерес).
"""
import bisect
import os
import pickle
import time
from array import array

import config

M15 = 900_000
DAY = 86_400_000
SUPPORTED = {"15": 1, "30": 2, "60": 4, "120": 8, "240": 16}   # во сколько 15-минуток укладывается свеча


# ------------------------------------------------------------------ загрузка
def download(live, days, log, cache_dir="data/history", fresh=False, until=None):
    """Скачать историю за days дней (до даты until, мс; по умолчанию — до сейчас).
    Кэш живёт 12 часов — повторный запуск не качает заново."""
    os.makedirs(cache_dir, exist_ok=True)
    tag = "" if until is None else time.strftime("_until%Y%m%d", time.gmtime(until / 1000))
    path = os.path.join(cache_dir, f"history_{days}d{tag}.pkl")
    if not fresh and os.path.exists(path) and time.time() - os.path.getmtime(path) < 12 * 3600:
        log(f"Беру историю из кэша ({path}). Чтобы скачать заново — добавь --fresh")
        with open(path, "rb") as f:
            return pickle.load(f)

    end = int(until if until is not None else time.time() * 1000) // M15 * M15
    start = end - days * DAY
    warm = start - 4 * DAY
    log(f"Скачиваю список фьючерсов…")
    launch = {i["symbol"]: int(float(i.get("launchTime") or 0)) for i in live.instruments()
              if i.get("symbol", "").endswith("USDT") and i.get("status", "Trading") == "Trading"}
    log(f"Фьючерсов USDT: {len(launch)}. Скачиваю дневные свечи, чтобы отобрать ликвидные монеты…")
    daily, keep = {}, []
    for n, s in enumerate(sorted(launch), 1):
        try:
            rows = live.klines(s, "D", min(1000, days + 160), end=end)
        except Exception as e:
            log(f"  {s}: пропуск ({e})")
            continue
        rows = sorted(([int(r[0])] + [float(x) for x in r[1:7]] for r in rows), key=lambda r: r[0])
        rows = [r if len(r) == 7 else r + [r[4] * r[5]] for r in rows]
        in_period = [r for r in rows if r[0] >= start]
        if not in_period:
            continue
        avg_turn = sum(r[6] for r in in_period) / len(in_period)
        if s in config.MAJORS or avg_turn >= 1e6:
            daily[s] = rows
            keep.append(s)
        if n % 100 == 0:
            log(f"  проверено {n}/{len(launch)}, ликвидных {len(keep)}")
    log(f"Ликвидных монет (оборот от $1 млн в день): {len(keep)}. Качаю 15-минутные свечи и фандинг — это несколько минут…")
    m15, funding = {}, {}
    t0 = time.time()
    for n, s in enumerate(keep, 1):
        try:
            rows = live.klines_range(s, "15", max(warm, launch.get(s, 0) // M15 * M15), end, M15)
            fund = live.funding_history(s, warm, end)
        except Exception as e:
            log(f"  {s}: пропуск ({e})")
            continue
        rows = [[int(r[0])] + [float(x) for x in r[1:7]] for r in rows if int(r[0]) < end]
        if len(rows) < 20:
            continue
        m15[s] = {k: array("d", [r[i] for r in rows]) for i, k in enumerate("tohlcvq")}
        funding[s] = fund
        if n % 25 == 0:
            left = (time.time() - t0) / n * (len(keep) - n)
            log(f"  {n}/{len(keep)} монет, осталось ~{left / 60:.0f} мин")
    data = {"start": start, "end": end, "days": days, "launch": launch, "daily": daily, "m15": m15,
            "funding": funding}
    with open(path, "wb") as f:
        pickle.dump(data, f, protocol=pickle.HIGHEST_PROTOCOL)
    log(f"История сохранена: {len(m15)} монет, {path}")
    return data


# ------------------------------------------------------------------ «машина времени»
class _Series:
    def __init__(self, d):
        self.t = [int(x) for x in d["t"]]
        self.o, self.h, self.l, self.c, self.v, self.q = d["o"], d["h"], d["l"], d["c"], d["v"], d["q"]
        self.cv, self.cq = [0.0], [0.0]           # накопленные суммы — для оборота за 24 ч за O(1)
        for v, q in zip(self.v, self.q):
            self.cv.append(self.cv[-1] + v)
            self.cq.append(self.cq[-1] + q)
        self.ptr = -1                              # индекс текущей (ещё не закрытой) свечи


class BacktestClient:
    """Подменяет BybitClient. Внутри каждой 15-минутной свечи цена проходит 3 точки: открытие → один экстремум →
    другой (для зелёной свечи сначала минимум, потом максимум; для красной наоборот). Так стопы и тейки
    срабатывают внутри свечи, а не только по закрытию. Закрытие свечи = открытие следующей."""

    def __init__(self, data, live, now_fn):
        self.live, self.now = live, now_fn
        self.start, self.end = data["start"], data["end"]
        self.launch = {s: lt for s, lt in data["launch"].items() if s in data["m15"]}
        self.daily = data["daily"]
        self.series = {s: _Series(d) for s, d in data["m15"].items()}
        self.fund = {s: ([t for t, _ in f], [r for _, r in f]) for s, f in data["funding"].items()}
        self.price = {}
        self._oi = {}

    # ----- движение времени
    def set_subtick(self, T, k):
        """T — время открытия текущей 15-минутной свечи (мс), k = 0 открытие, 1 и 2 — экстремумы."""
        for s, se in self.series.items():
            while se.ptr + 1 < len(se.t) and se.t[se.ptr + 1] <= T:
                se.ptr += 1
            i = se.ptr
            if i < 0:
                continue
            if se.t[i] != T:                 # торгов в этой свече не было — цена прежняя (последнее закрытие)
                self.price[s] = se.c[i]
                continue
            o, h, l, c = se.o[i], se.h[i], se.l[i], se.c[i]
            first, second = (l, h) if c >= o else (h, l)
            self.price[s] = (o, first, second)[k]

    def set_final(self):
        for s, se in self.series.items():
            if se.ptr >= 0:
                self.price[s] = se.c[se.ptr]

    # ----- то, что видят боты
    def tickers(self):
        now_ms = int(self.now() * 1000)
        out = []
        for s, se in self.series.items():
            if s not in self.price:
                continue
            i = se.ptr if se.t[se.ptr] <= now_ms - M15 else se.ptr - 1   # последняя закрытая свеча
            n_closed = i + 1
            j = max(0, n_closed - 96)
            ref = se.c[j - 1] if j > 0 else se.o[0]
            px = self.price[s]
            fts, frs = self.fund.get(s, ([], []))
            k = bisect.bisect_right(fts, now_ms)
            if fts:
                rate = frs[k] if k < len(frs) else frs[-1]
                nft = fts[k] if k < len(fts) else fts[-1] + 8 * 3600 * 1000 * (1 + (now_ms - fts[-1]) // (8 * 3600 * 1000))
            else:
                rate, nft = 0.0001, (now_ms // (8 * 3600 * 1000) + 1) * 8 * 3600 * 1000
            out.append({"symbol": s, "lastPrice": px, "price24hPcnt": px / ref - 1 if ref else 0,
                        "turnover24h": se.cq[n_closed] - se.cq[j], "volume24h": se.cv[n_closed] - se.cv[j],
                        "fundingRate": rate, "nextFundingTime": nft, "openInterestValue": 0,
                        "highPrice24h": px, "lowPrice24h": px})
        return out

    def instruments(self):
        now_ms = int(self.now() * 1000)
        return [{"symbol": s, "launchTime": lt, "status": "Trading"} for s, lt in self.launch.items() if lt <= now_ms]

    def klines(self, symbol, interval, limit=200, start=None, end=None):
        now_ms = int(self.now() * 1000)
        cap = now_ms if end is None else min(now_ms, int(end) + INTERVAL_MS(interval))
        if interval == "D":
            rows = [r for r in self.daily.get(symbol, []) if r[0] + DAY <= cap and (start is None or r[0] >= start)]
            return [list(r) for r in reversed(rows[-limit:])]
        se = self.series.get(symbol)
        if interval not in SUPPORTED or se is None:
            return self.live.klines(symbol, interval, limit, start, end) if self.live else []
        ms = SUPPORTED[interval] * M15
        n = bisect.bisect_right(se.t, cap - M15)          # сколько 15-минуток закрыто к моменту cap
        out, i = [], n - 1
        while i >= 0 and len(out) < limit:
            b = se.t[i] // ms * ms
            if b + ms > cap:                               # старшая свеча ещё не закрылась
                while i >= 0 and se.t[i] >= b:
                    i -= 1
                continue
            if start is not None and b < start:
                break
            hi, lo, vol, turn, close, j = -1e300, 1e300, 0.0, 0.0, se.c[i], i
            while j >= 0 and se.t[j] >= b:
                hi, lo = max(hi, se.h[j]), min(lo, se.l[j])
                vol += se.v[j]
                turn += se.q[j]
                j -= 1
            out.append([b, se.o[j + 1], hi, lo, close, vol, turn])
            i = j
        return out

    def klines_range(self, symbol, interval, start_ms, end_ms, step_ms):
        if (interval in SUPPORTED and symbol in self.series) or interval == "D":
            return sorted(self.klines(symbol, interval, 10 ** 6, start=start_ms, end=end_ms), key=lambda r: r[0])
        return self.live.klines_range(symbol, interval, start_ms, end_ms, step_ms) if self.live else []

    def open_interest(self, symbol, interval_time="15min", limit=96, start=None, end=None, cursor=None):
        """Открытый интерес: при первом запросе по монете качается вся история за период, дальше — из памяти."""
        key = (symbol, interval_time)
        if key not in self._oi:
            try:
                self._oi[key] = self.live.open_interest_range(symbol, interval_time, self.start - 2 * DAY, self.end)
            except Exception:
                self._oi[key] = []
        now_ms = int(self.now() * 1000)
        pts = self._oi[key]
        k = bisect.bisect_right(pts, (now_ms, float("inf")))
        return [{"timestamp": t, "openInterest": v} for t, v in reversed(pts[max(0, k - limit):k])]

    def open_interest_range(self, symbol, interval_time, start_ms, end_ms):
        return self.live.open_interest_range(symbol, interval_time, start_ms, end_ms) if self.live else []


def INTERVAL_MS(interval):
    return DAY if interval == "D" else SUPPORTED.get(interval, 1) * M15 if interval in SUPPORTED else int(interval) * 60_000
