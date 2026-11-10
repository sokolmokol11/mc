"""База SQLite: сделки, решения ботов, история баланса, состояние ботов (чтобы после перезапуска всё продолжилось)."""
import json
import os
import sqlite3
import threading

EXTRA_COLS = {"lev": "REAL", "margin": "REAL", "stop": "REAL", "tp": "REAL", "last_stop": "REAL",
              "interval": "TEXT", "details": "TEXT", "market": "TEXT"}


class Storage:
    def __init__(self, path, fast=False):
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.lock = threading.Lock()
        if fast:   # бэктест: скорость важнее защиты от сбоя питания
            self.db.execute("PRAGMA synchronous=OFF")
            self.db.execute("PRAGMA journal_mode=MEMORY")
        with self.lock:
            self.db.executescript("""
            CREATE TABLE IF NOT EXISTS trades(
                id INTEGER PRIMARY KEY, bot TEXT, symbol TEXT, side TEXT, qty REAL,
                entry REAL, exit REAL, pnl REAL, pnl_pct REAL, fees REAL, funding REAL,
                opened_at REAL, closed_at REAL, reason_open TEXT, reason_close TEXT);
            CREATE TABLE IF NOT EXISTS equity(ts REAL, bot TEXT, equity REAL);
            CREATE INDEX IF NOT EXISTS eq_bot ON equity(bot, ts);
            CREATE TABLE IF NOT EXISTS state(bot TEXT PRIMARY KEY, data TEXT);
            CREATE TABLE IF NOT EXISTS decisions(
                id INTEGER PRIMARY KEY, ts REAL, bot TEXT, symbol TEXT, action TEXT,
                reason TEXT, details TEXT, interval TEXT);
            CREATE INDEX IF NOT EXISTS dec_ts ON decisions(ts);
            """)
            have = {r[1] for r in self.db.execute("PRAGMA table_info(trades)")}
            for col, typ in EXTRA_COLS.items():   # обновление базы от старой версии программы
                if col not in have:
                    self.db.execute(f'ALTER TABLE trades ADD COLUMN "{col}" {typ}')
            self.db.commit()

    # ---------- запись ----------
    def add_trade(self, **t):
        if "details" in t:
            t["details"] = json.dumps(t["details"], ensure_ascii=False)
        cols = ",".join(f'"{k}"' for k in t)
        q = ",".join("?" * len(t))
        with self.lock:
            self.db.execute(f"INSERT INTO trades({cols}) VALUES({q})", list(t.values()))
            self.db.commit()

    def add_decision(self, ts, bot, symbol, action, reason, details, interval):
        with self.lock:
            self.db.execute("INSERT INTO decisions(ts,bot,symbol,action,reason,details,interval) VALUES(?,?,?,?,?,?,?)",
                            (ts, bot, symbol, action, reason, json.dumps(details, ensure_ascii=False), interval))
            self.db.commit()

    def prune_decisions(self, older_than_ts):
        with self.lock:
            self.db.execute("DELETE FROM decisions WHERE ts < ? AND action = 'skip'", (older_than_ts,))
            self.db.commit()

    def add_equity(self, ts, values):
        with self.lock:
            self.db.executemany("INSERT INTO equity VALUES(?,?,?)", [(ts, b, e) for b, e in values.items()])
            self.db.commit()

    def save_state(self, bot, data):
        with self.lock:
            self.db.execute("INSERT OR REPLACE INTO state VALUES(?,?)", (bot, json.dumps(data)))
            self.db.commit()

    def load_state(self, bot):
        with self.lock:
            row = self.db.execute("SELECT data FROM state WHERE bot=?", (bot,)).fetchone()
        return json.loads(row[0]) if row else None

    # ---------- чтение ----------
    def _rows(self, cur):
        names = [d[0] for d in cur.description]
        out = []
        for r in cur.fetchall():
            d = dict(zip(names, r))
            try:
                d["details"] = json.loads(d.get("details") or "{}")
            except ValueError:
                d["details"] = {}
            out.append(d)
        return out

    def trades(self, limit=200, bot=None, since=None):
        where, args = [], []
        if bot:
            where.append("bot=?")
            args.append(bot)
        if since:
            where.append("closed_at>=?")
            args.append(since)
        sql = "SELECT * FROM trades " + ("WHERE " + " AND ".join(where) + " " if where else "") + \
              "ORDER BY closed_at DESC LIMIT ?"
        with self.lock:
            return self._rows(self.db.execute(sql, args + [limit]))

    def decisions(self, limit=200, bot=None, action=None, since=None):
        where, args = [], []
        for col, val in (("bot", bot), ("action", action)):
            if val:
                where.append(f"{col}=?")
                args.append(val)
        if since:
            where.append("ts>=?")
            args.append(since)
        sql = "SELECT * FROM decisions " + ("WHERE " + " AND ".join(where) + " " if where else "") + \
              "ORDER BY ts DESC LIMIT ?"
        with self.lock:
            return self._rows(self.db.execute(sql, args + [limit]))

    def decision_reasons(self, since=None):
        """Сколько раз каждый бот пропускал монеты и по какой причине."""
        with self.lock:
            rows = self.db.execute(
                "SELECT bot, reason, COUNT(*) FROM decisions WHERE action='skip' AND ts>=? GROUP BY bot, reason "
                "ORDER BY bot, COUNT(*) DESC", (since or 0,)).fetchall()
        out = {}
        for bot, reason, n in rows:
            out.setdefault(bot, []).append((reason, n))
        return out

    def bot_stats(self, since=None):
        """Статистика по каждому боту. Частичные закрытия одной позиции считаются одной сделкой."""
        with self.lock:
            rows = self.db.execute(
                "SELECT bot, SUM(pnl), MIN(opened_at), MAX(closed_at), SUM(margin) FROM trades WHERE closed_at>=? "
                "GROUP BY bot, symbol, opened_at", (since or 0,)).fetchall()
        stats = {}
        for bot, pnl, opened, closed, margin in rows:
            s = stats.setdefault(bot, {"trades": 0, "wins": 0, "win_sum": 0.0, "loss_sum": 0.0,
                                       "hold": 0.0, "best": None, "worst": None, "realized": 0.0})
            s["trades"] += 1
            s["realized"] += pnl
            s["hold"] += closed - opened
            if pnl > 0:
                s["wins"] += 1
                s["win_sum"] += pnl
            else:
                s["loss_sum"] += pnl
            s["best"] = pnl if s["best"] is None else max(s["best"], pnl)
            s["worst"] = pnl if s["worst"] is None else min(s["worst"], pnl)
        for s in stats.values():
            n, w = s["trades"], s["wins"]
            s["winrate"] = w / n * 100 if n else None
            s["avg_win"] = s["win_sum"] / w if w else None
            s["avg_loss"] = s["loss_sum"] / (n - w) if n - w else None
            s["profit_factor"] = s["win_sum"] / -s["loss_sum"] if s["loss_sum"] < 0 else None
            s["expectancy"] = s["realized"] / n if n else None
            s["avg_hold_h"] = s["hold"] / n / 3600 if n else None
        return stats

    def trade_stats(self):
        return self.bot_stats()

    def max_drawdown(self, since=None):
        """Максимальная просадка баланса от пика, % — по точкам истории баланса."""
        with self.lock:
            rows = self.db.execute("SELECT bot, equity FROM equity WHERE ts>=? ORDER BY ts", (since or 0,)).fetchall()
        peak, dd = {}, {}
        for bot, eq in rows:
            peak[bot] = max(peak.get(bot, eq), eq)
            dd[bot] = max(dd.get(bot, 0.0), (1 - eq / peak[bot]) * 100 if peak[bot] else 0)
        return dd

    def trade(self, trade_id):
        with self.lock:
            rows = self._rows(self.db.execute("SELECT * FROM trades WHERE id=?", (trade_id,)))
        return rows[0] if rows else None

    def equity_history(self, max_points=400):
        with self.lock:
            rows = self.db.execute("SELECT ts, bot, equity FROM equity ORDER BY ts").fetchall()
        series = {}
        for ts, bot, eq in rows:
            series.setdefault(bot, []).append([ts, eq])
        for bot, pts in series.items():
            if len(pts) > max_points:
                step = len(pts) / max_points
                series[bot] = [pts[int(i * step)] for i in range(max_points)] + [pts[-1]]
        return series
