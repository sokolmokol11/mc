"""Получение публичных данных с Bybit (без ключей и регистрации)."""
import threading
import time
import requests

RATE_LIMIT_CODES = {10006, 10018}


class BybitClient:
    MIN_INTERVAL = 0.06   # не чаще ~16 запросов в секунду — с большим запасом от лимитов Bybit

    def __init__(self, base_urls, proxy=""):
        self.urls = list(base_urls)
        self.i = 0
        self.session = requests.Session()
        self.session.headers["User-Agent"] = "trading-sim/1.0"
        if proxy:
            self.session.proxies = {"http": proxy, "https": proxy}
        self._lock = threading.Lock()
        self._last = 0.0

    def _get(self, path, params):
        last_err = None
        for attempt in range(4):
            with self._lock:
                wait = self.MIN_INTERVAL - (time.time() - self._last)
                if wait > 0:
                    time.sleep(wait)
                self._last = time.time()
            url = self.urls[self.i] + path
            try:
                r = self.session.get(url, params=params, timeout=10)
                data = r.json()
            except Exception as e:  # сеть, таймаут, неверный ответ
                last_err = str(e)
                self.i = (self.i + 1) % len(self.urls)
                time.sleep(1 + attempt)
                continue
            if data.get("retCode") == 0:
                return data["result"]
            last_err = f"код {data.get('retCode')}: {data.get('retMsg')}"
            if data.get("retCode") not in RATE_LIMIT_CODES:
                break                      # ошибка в запросе — повторять бессмысленно
            time.sleep(2 + 2 * attempt)
        raise RuntimeError(f"Bybit: {last_err}")

    def tickers(self):
        """Все бессрочные фьючерсы USDT: цена, изменение за 24ч, объём, фандинг."""
        return self._get("/v5/market/tickers", {"category": "linear"})["list"]

    def instruments(self):
        """Список всех фьючерсов с датой запуска (launchTime)."""
        out, cursor = [], None
        for _ in range(10):
            params = {"category": "linear", "limit": 1000}
            if cursor:
                params["cursor"] = cursor
            res = self._get("/v5/market/instruments-info", params)
            out += res.get("list", [])
            cursor = res.get("nextPageCursor")
            if not cursor:
                break
        return out

    def klines(self, symbol, interval, limit=200, start=None, end=None):
        """Свечи. Bybit отдаёт от новой к старой: [start, open, high, low, close, volume, turnover]."""
        params = {"category": "linear", "symbol": symbol, "interval": interval, "limit": limit}
        if start is not None:
            params["start"] = int(start)
        if end is not None:
            params["end"] = int(end)
        return self._get("/v5/market/kline", params)["list"]

    def klines_range(self, symbol, interval, start_ms, end_ms, step_ms):
        """Все свечи в промежутке (несколькими запросами по 1000 штук), от старой к новой."""
        rows, end = {}, int(end_ms)
        for _ in range(10):
            batch = self.klines(symbol, interval, 1000, start=start_ms, end=end)
            if not batch:
                break
            for b in batch:
                rows[int(b[0])] = b
            oldest = min(int(b[0]) for b in batch)
            if oldest <= start_ms or len(batch) < 1000:
                break
            end = oldest - step_ms
        return [rows[k] for k in sorted(rows)]

    def open_interest(self, symbol, interval_time="15min", limit=96, start=None, end=None, cursor=None):
        """Открытый интерес: [{"openInterest": "...", "timestamp": "..."}], от нового к старому."""
        params = {"category": "linear", "symbol": symbol, "intervalTime": interval_time, "limit": min(limit, 200)}
        if start is not None:
            params["startTime"] = int(start)
        if end is not None:
            params["endTime"] = int(end)
        if cursor:
            params["cursor"] = cursor
        res = self._get("/v5/market/open-interest", params)
        if cursor is None and start is None:
            return res.get("list", [])
        return res

    def funding_history(self, symbol, start_ms, end_ms):
        """История выплат фандинга: [(ts_ms, rate)], от старой к новой."""
        pts, end = {}, int(end_ms)
        for _ in range(15):
            res = self._get("/v5/market/funding/history", {"category": "linear", "symbol": symbol,
                                                            "startTime": int(start_ms), "endTime": end, "limit": 200})
            lst = res.get("list", [])
            for r in lst:
                pts[int(r["fundingRateTimestamp"])] = float(r["fundingRate"])
            if len(lst) < 200:
                break
            end = min(int(r["fundingRateTimestamp"]) for r in lst) - 1
        return sorted(pts.items())

    def open_interest_range(self, symbol, interval_time, start_ms, end_ms):
        """Открытый интерес за промежуток, от старого к новому: [(ts_ms, oi)]."""
        pts, cursor = {}, None
        for _ in range(15):
            res = self.open_interest(symbol, interval_time, 200, start=start_ms, end=end_ms, cursor=cursor or "")
            for r in res.get("list", []):
                pts[int(r["timestamp"])] = float(r["openInterest"])
            cursor = res.get("nextPageCursor")
            if not cursor or not res.get("list"):
                break
        return sorted(pts.items())
