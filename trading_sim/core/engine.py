"""Главный цикл: берёт цены, проверяет стопы, даёт каждому боту подумать, сохраняет всё."""
import threading
import time
import traceback
from collections import deque
from datetime import datetime

import config
from core.broker import Account
from core.market import Market


class Ctx:
    def __init__(self, market, acc, now, log, decide):
        self.market, self.acc, self.now, self.log, self.decide = market, acc, now, log, decide


class Engine:
    def __init__(self, client, storage, bot_classes, now_fn=time.time, quiet=False):
        self.now = now_fn
        self.storage = storage
        self.quiet = quiet
        self.journal = deque(maxlen=300)
        self.lock = threading.RLock()
        self.market = Market(client, now_fn, self.log)
        self.started = now_fn()
        self.last_snapshot = 0
        self.last_error = None
        self.last_prune = 0
        self.mode = None           # для бэктеста — подпись периода
        self.bots, self.accounts = [], {}
        for cls in bot_classes:
            bot = cls()
            acc = Account(bot.name, storage, self.log, bot.title, config.LEVERAGE.get(bot.name, 1))
            saved = storage.load_state(bot.name)
            if saved:
                acc.load(saved.get("account", {}))
                bot.state = saved.get("bot", {})
                self.started = min(self.started, saved.get("started", self.started))
            acc.on_close = self._closer(bot)
            self.bots.append(bot)
            self.accounts[bot.name] = acc

    def log(self, msg):
        line = f"{datetime.fromtimestamp(self.now()).strftime('%d.%m %H:%M:%S')}  {msg}"
        self.journal.appendleft(line)
        if not self.quiet:
            print(line, flush=True)

    def tick(self):
        with self.lock:
            now = self.now()
            try:
                funding_events = self.market.refresh()
                self.last_error = None
            except Exception as e:
                self.last_error = str(e)
                self.log(f"⚠ нет связи с Bybit: {e}. Пробую снова через {config.TICK_SECONDS} сек.")
                return
            for sym, rate in funding_events:
                px = self.market.price(sym)
                for acc in self.accounts.values():
                    acc.apply_funding(sym, rate, px)
            for bot in self.bots:
                acc = self.accounts[bot.name]
                acc.check_exits(self.market.price, now)
                try:
                    bot.on_tick(Ctx(self.market, acc, now, self.log, self._decider(bot, now)))
                except Exception:
                    self.log(f"⚠ ошибка в боте {bot.title}:\n{traceback.format_exc()}")
                self.storage.save_state(bot.name, {"account": acc.to_dict(), "bot": bot.state,
                                                   "started": self.started})
            if now - self.last_prune > 86400:     # пропуски старше 60 дней удаляем, чтобы база не разрасталась
                self.last_prune = now
                self.storage.prune_decisions(now - 60 * 86400)
            if now - self.last_snapshot >= config.EQUITY_SNAPSHOT_SECONDS:
                self.last_snapshot = now
                eq = {b.name: self.accounts[b.name].equity(self.market.price) for b in self.bots}
                self.storage.add_equity(now, eq)
                if not self.quiet:
                    self.print_summary()

    def _closer(self, bot):
        def on_close(pos, net, reason, fraction, fill):
            part = "" if fraction >= 0.999 else f" ({fraction:.0%} позиции)"
            self.storage.add_decision(self.now(), bot.name, pos.symbol, "exit", reason + part,
                                      {"Цена выхода": f"{fill:.6g}", "Прибыль": f"{net:+.2f}$",
                                       "_opened_at": pos.opened_at, "_side": pos.side}, pos.interval)
        return on_close

    def _decider(self, bot, now):
        def decide(symbol, action, reason, details=None, interval=None):
            self.storage.add_decision(now, bot.name, symbol, action, reason, details or {},
                                      interval or bot.chart_interval)
        return decide

    def summary(self):
        with self.lock:
            stats = self.storage.bot_stats()
            dd = self.storage.max_drawdown()
            rows = []
            for b in self.bots:
                acc = self.accounts[b.name]
                eq = acc.equity(self.market.price)
                st = stats.get(b.name, {"trades": 0})
                positions = []
                for p in acc.positions.values():
                    px = self.market.price(p.symbol) or p.entry
                    positions.append({
                        "bot": b.name, "key": p.key, "symbol": p.symbol, "side": p.side, "entry": p.entry,
                        "price": px, "margin": p.margin, "lev": p.lev, "pnl": p.pnl(px),
                        "pnl_pct": p.pnl(px) / p.margin * 100 if p.margin else 0,
                        "stop": p.stop, "tp": p.tp, "first_stop": p.first_stop, "first_tp": p.first_tp,
                        "liq": p.liq_price(), "opened_at": p.opened_at, "reason_open": p.reason,
                        "interval": p.interval, "details": p.details, "market": p.market})
                rows.append({
                    "name": b.name, "title": b.title, "timeframe": b.timeframe, "description": b.description,
                    "rules": b.rules, "lev": acc.lev, "symbols_text": b.symbols_text(), "control": b.control,
                    "equity": eq, "pnl_pct": (eq / config.START_BALANCE - 1) * 100,
                    "trades": st["trades"], "winrate": st.get("winrate"), "profit_factor": st.get("profit_factor"),
                    "avg_win": st.get("avg_win"), "avg_loss": st.get("avg_loss"), "expectancy": st.get("expectancy"),
                    "avg_hold_h": st.get("avg_hold_h"), "max_dd": dd.get(b.name, 0.0),
                    "positions": positions})
            rows.sort(key=lambda r: -r["equity"])
            return {"bots": rows, "started": self.started, "now": self.now(), "mode": self.mode,
                    "error": self.last_error, "journal": list(self.journal)[:80]}

    def print_summary(self):
        s = self.summary()
        print("\n" + "─" * 64)
        print(f"{'Бот':<22}{'Баланс':>11}{'Итог':>9}{'Сделок':>8}{'Открыто':>9}")
        for r in s["bots"]:
            print(f"{r['title']:<22}{r['equity']:>10.2f}${r['pnl_pct']:>+8.2f}%{r['trades']:>8}{len(r['positions']):>9}")
        print("─" * 64 + f"\nСтраница с результатами: http://localhost:{config.DASHBOARD_PORT}\n", flush=True)

    def run_forever(self):
        self.log(f"Запущено ботов: {len(self.bots)}. Цены обновляются каждые {config.TICK_SECONDS} сек.")
        while True:
            t0 = time.time()
            self.tick()
            time.sleep(max(1.0, config.TICK_SECONDS - (time.time() - t0)))
