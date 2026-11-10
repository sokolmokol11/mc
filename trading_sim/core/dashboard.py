"""Маленький веб-сервер для страницы с результатами (http://localhost:8000)."""
import base64
import hmac
import json
import os
import threading
import time
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs

from core import overlays
from core.market import Candles, INTERVAL_MS, OI_INTERVAL
from core.report import build_report

HTML_PATH = os.path.join(os.path.dirname(__file__), "dashboard.html")
STEPS = ["1", "3", "5", "15", "30", "60", "120", "240", "D"]
WARMUP = 250        # свечей до начала графика — чтобы индикаторы (EMA 200 и т.п.) успели «разогнаться»
MAX_VISIBLE = 700   # если сделка длинная — график автоматически переходит на более крупные свечи


def _oi_series(client, symbol, interval, start_ms, end_ms, times):
    """Открытый интерес, подогнанный под время свечей (последнее известное значение на каждую свечу)."""
    it = OI_INTERVAL.get(interval, "1h")
    try:
        pts = client.open_interest_range(symbol, it, start_ms, end_ms)
    except Exception:
        return None
    if not pts:
        return None
    out, j, last = [], 0, None
    for t in times:
        while j < len(pts) and pts[j][0] <= t:
            last = pts[j][1]
            j += 1
        out.append(last)
    return out


def chart_data(engine, client, bot_name, symbol, interval, t_from, t_to, anchor=None):
    if interval not in INTERVAL_MS:
        interval = "15"
    while (t_to - t_from) * 1000 / INTERVAL_MS[interval] > MAX_VISIBLE and interval != "D":
        interval = STEPS[STEPS.index(interval) + 1]
    ms = INTERVAL_MS[interval]
    start = int(t_from * 1000) - WARMUP * ms
    rows = client.klines_range(symbol, interval, start, int(t_to * 1000), ms)
    if not rows:
        return {"interval": interval, "candles": [], "overlays": []}
    cd = Candles(rows)
    bot = next((b for b in engine.bots if b.name == bot_name), None)
    names = bot.overlays if bot else []
    lines = overlays.build(names, cd, ms / 1000, int(anchor * 1000) if anchor else None)
    first = next((i for i, t in enumerate(cd.t) if t >= t_from * 1000), 0)
    if "oi" in names:
        oi = _oi_series(client, symbol, interval, cd.t[first], cd.t[-1] + ms, cd.t)
        if oi:
            lines.append({"label": "Открытый интерес (OI)", "color": "#14b8a6", "pane": "oi", "width": 2,
                          "type": "line", "data": oi})
    times = [t // 1000 for t in cd.t]
    candles = [{"time": times[i], "open": cd.o[i], "high": cd.h[i], "low": cd.l[i], "close": cd.c[i],
                "volume": cd.v[i]} for i in range(first, len(cd))]
    for ln in lines:
        ln["data"] = [{"time": times[i], "value": ln["data"][i]} if ln["data"][i] is not None else {"time": times[i]}
                      for i in range(first, len(cd))]
    return {"interval": interval, "candles": candles, "overlays": lines}


def start_dashboard(engine, port, chart_client=None, host="127.0.0.1", user="", password=""):
    client = chart_client or engine.market.client
    lock = threading.Lock()
    token = "Basic " + base64.b64encode(f"{user}:{password}".encode()).decode() if password else None

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _authorized(self):
            if token is None or hmac.compare_digest(self.headers.get("Authorization", ""), token):
                return True
            self.send_response(401)
            self.send_header("WWW-Authenticate", 'Basic realm="trading-sim", charset="UTF-8"')
            self.send_header("Content-Length", "0")
            self.end_headers()
            return False

        def _send(self, body, ctype="application/json", code=200):
            data = body.encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", ctype + "; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            if not self._authorized():
                return
            u = urlparse(self.path)
            q = {k: v[0] for k, v in parse_qs(u.query).items()}
            try:
                if u.path == "/":
                    with open(HTML_PATH, encoding="utf-8") as f:
                        self._send(f.read(), "text/html")
                elif u.path == "/api/summary":
                    self._send(json.dumps(engine.summary()))
                elif u.path == "/api/equity":
                    self._send(json.dumps(engine.storage.equity_history()))
                elif u.path == "/api/trades":
                    self._send(json.dumps(engine.storage.trades(200, q.get("bot"))))
                elif u.path == "/api/chart":
                    with lock:
                        data = chart_data(engine, client, q["bot"], q["symbol"], q.get("interval", "15"),
                                          float(q["from"]), min(float(q["to"]), engine.now()),
                                          float(q["anchor"]) if q.get("anchor") else None)
                    self._send(json.dumps(data))
                elif u.path == "/api/decisions":
                    self._send(json.dumps(engine.storage.decisions(int(q.get("limit", 300)), q.get("bot"),
                                                                   q.get("action"))))
                elif u.path == "/api/report":
                    days = max(1, min(365, int(q.get("days", 7))))
                    with engine.lock:
                        text = build_report(engine, days, q.get("bot"))
                    data = text.encode("utf-8")
                    name = time.strftime("report_%Y-%m-%d", time.localtime(engine.now())) + f"_{days}d.md"
                    self.send_response(200)
                    self.send_header("Content-Type", "text/markdown; charset=utf-8")
                    self.send_header("Content-Disposition", f'attachment; filename="{name}"')
                    self.send_header("Content-Length", str(len(data)))
                    self.end_headers()
                    self.wfile.write(data)
                else:
                    self.send_error(404)
            except Exception as e:
                self._send(json.dumps({"error": str(e)}), code=500)

    server = ThreadingHTTPServer((host, port), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server
