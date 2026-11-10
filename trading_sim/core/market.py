"""Рыночные данные: текущие цены всех монет, свечи, открытый интерес, даты запуска фьючерсов."""

INTERVAL_MS = {
    "1": 60_000, "3": 180_000, "5": 300_000, "15": 900_000, "30": 1_800_000,
    "60": 3_600_000, "120": 7_200_000, "240": 14_400_000, "D": 86_400_000,
}
# интервалы открытого интереса у Bybit
OI_INTERVAL = {"1": "5min", "3": "5min", "5": "5min", "15": "15min", "30": "30min",
               "60": "1h", "120": "1h", "240": "4h", "D": "1d"}
OI_MS = {"5min": 300_000, "15min": 900_000, "30min": 1_800_000, "1h": 3_600_000, "4h": 14_400_000, "1d": 86_400_000}

STABLES = {"USDC", "USDE", "FDUSD", "DAI", "TUSD", "USDD", "PYUSD", "BUSD", "USTC", "EURC", "USD1", "RLUSD"}


class Candles:
    """Закрытые свечи, от старой к новой. t — время открытия свечи (мс)."""

    def __init__(self, rows):
        self.t = [int(r[0]) for r in rows]
        self.o = [float(r[1]) for r in rows]
        self.h = [float(r[2]) for r in rows]
        self.l = [float(r[3]) for r in rows]
        self.c = [float(r[4]) for r in rows]
        self.v = [float(r[5]) for r in rows]
        self.requested = len(rows)

    def __len__(self):
        return len(self.c)


def _f(x, default=0.0):
    try:
        return float(x)
    except (TypeError, ValueError):
        return default


def base_coin(symbol):
    s = symbol[:-4] if symbol.endswith("USDT") else symbol
    for p in ("1000000", "10000", "1000"):
        if s.startswith(p):
            return s[len(p):]
    return s


class Market:
    def __init__(self, client, now_fn, log):
        self.client = client
        self.now = now_fn
        self.log = log
        self.tickers = {}
        self._cache = {}
        self._oi_cache = {}
        self._funding_seen = {}   # symbol -> (nextFundingTime, rate)
        self._launch = {}
        self._launch_at = 0

    def refresh(self):
        """Обновить цены. Возвращает список выплат фандинга [(symbol, rate)], прошедших с прошлого раза."""
        raw = self.client.tickers()
        events = []
        fresh = {}
        for t in raw:
            sym = t.get("symbol", "")
            last = _f(t.get("lastPrice"))
            if not sym.endswith("USDT") or last <= 0:
                continue
            nft = int(_f(t.get("nextFundingTime")))
            rate = _f(t.get("fundingRate"))
            fresh[sym] = {
                "symbol": sym,
                "last": last,
                "pct24h": _f(t.get("price24hPcnt")),
                "turnover24h": _f(t.get("turnover24h")),
                "volume24h": _f(t.get("volume24h")),
                "funding": rate,
                "next_funding": nft,
                "oi_value": _f(t.get("openInterestValue")),
                "high24h": _f(t.get("highPrice24h")),
                "low24h": _f(t.get("lowPrice24h")),
            }
            prev = self._funding_seen.get(sym)
            if prev and nft > prev[0] > 0:
                events.append((sym, prev[1]))   # прошла выплата по ставке, что действовала до неё
            self._funding_seen[sym] = (nft, rate)
        self.tickers = fresh
        return events

    def price(self, symbol):
        t = self.tickers.get(symbol)
        return t["last"] if t else None

    def is_stable(self, symbol):
        return base_coin(symbol) in STABLES

    def candles(self, symbol, interval, limit=200):
        """Закрытые свечи. Перезапрашиваются только когда закрылась новая свеча."""
        key = (symbol, interval)
        ms = INTERVAL_MS[interval]
        now_ms = int(self.now() * 1000)
        cached = self._cache.get(key)
        if cached and cached.requested >= limit and now_ms < cached.t[-1] + 2 * ms + 3000:
            return cached
        want = max(limit, cached.requested if cached else 0)
        try:
            raw = self.client.klines(symbol, interval, min(want, 1000))
        except Exception as e:
            self.log(f"⚠ свечи {symbol} {interval} не получены: {e}")
            return cached
        rows = sorted(raw, key=lambda r: int(r[0]))
        rows = [r for r in rows if int(r[0]) + ms <= now_ms]   # только закрытые
        if not rows:
            return cached
        c = Candles(rows)
        c.requested = want
        self._cache[key] = c
        return c

    def open_interest(self, symbol, interval_time="15min", limit=96):
        """Открытый интерес (сколько монет в открытых позициях), от старого к новому: [(ts_ms, oi)]."""
        key = (symbol, interval_time)
        step = OI_MS[interval_time]
        now_ms = int(self.now() * 1000)
        cached = self._oi_cache.get(key)
        if cached and cached[0] >= limit and now_ms < cached[1][-1][0] + 2 * step + 3000:
            return cached[1]
        try:
            raw = self.client.open_interest(symbol, interval_time, limit)
        except Exception as e:
            self.log(f"⚠ открытый интерес {symbol} не получен: {e}")
            return cached[1] if cached else []
        pts = sorted((int(r["timestamp"]), _f(r["openInterest"])) for r in raw)
        if pts:
            self._oi_cache[key] = (limit, pts)
        return pts

    def launch_times(self):
        """Дата запуска каждого фьючерса (мс). Обновляется раз в час."""
        if self.now() - self._launch_at > 3600 or not self._launch:
            try:
                self._launch = {i["symbol"]: int(_f(i.get("launchTime"))) for i in self.client.instruments()
                                if i.get("symbol", "").endswith("USDT") and i.get("status", "Trading") == "Trading"}
                self._launch_at = self.now()
            except Exception as e:
                self.log(f"⚠ список фьючерсов не получен: {e}")
        return self._launch

    def age_days(self, symbol):
        lt = self.launch_times().get(symbol)
        return (self.now() * 1000 - lt) / 86_400_000 if lt else None
