"""Бэктест: прогнать всех ботов через реальную историю Bybit за прошлые дни.

Запуск на сервере:
    python3.11 backtest.py                 # последние 30 дней
    python3.11 backtest.py --days 90       # последние 90 дней (до 90)
    python3.11 backtest.py --serve         # после прогона открыть страницу с результатами на порту 8001
    python3.11 backtest.py --fresh         # скачать историю заново (иначе берётся кэш, если ему меньше 12 часов)

Результаты: страница (с --serve), файл отчёта data/backtest_report_*.md и база data/backtest.db.
Живой симулятор при этом продолжает работать — бэктест его не трогает.
"""
import argparse
import os
import sys
import time

os.chdir(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.getcwd())
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass


def load_env(path=".env"):
    if os.path.exists(path):
        for line in open(path, encoding="utf-8"):
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip())


load_env()

import config  # noqa: E402  (после .env — чтобы подхватить прокси и пароль)
from bots import ALL_BOTS  # noqa: E402
from core.engine import Engine  # noqa: E402
from core.history import BacktestClient, download, M15  # noqa: E402
from core.report import build_report  # noqa: E402
from core.storage import Storage  # noqa: E402


def log(msg):
    print(time.strftime("%H:%M:%S"), msg, flush=True)


def run(live, days=30, fresh=False, db_path="data/backtest.db", cache_dir="data/history", quiet_progress=False):
    data = download(live, days, log, cache_dir, fresh)
    start, end = data["start"], data["end"]
    clock = [start / 1000]
    client = BacktestClient(data, live, lambda: clock[0])
    for suffix in ("", "-journal", "-wal"):
        if os.path.exists(db_path + suffix):
            os.remove(db_path + suffix)
    storage = Storage(db_path, fast=True)
    engine = Engine(client, storage, ALL_BOTS, now_fn=lambda: clock[0], quiet=True)
    engine.started = start / 1000
    engine.mode = f"бэктест {time.strftime('%d.%m', time.localtime(start / 1000))}–" \
                  f"{time.strftime('%d.%m.%Y', time.localtime(end / 1000))}"
    log(f"Прокручиваю {days} дней через {len(engine.bots)} ботов…")
    t0, day = time.time(), 0
    for T in range(start, end, M15):
        for k in range(3):
            clock[0] = T / 1000 + k * 300
            client.set_subtick(T, k)
            engine.tick()
        if (T - start) // 86_400_000 > day:
            day = (T - start) // 86_400_000
            if not quiet_progress and day % 3 == 0:
                eq = sorted(((engine.accounts[b.name].equity(engine.market.price), b.title) for b in engine.bots), reverse=True)
                log(f"  день {day}/{days} ({time.time() - t0:.0f} сек): лидер {eq[0][1]} ${eq[0][0]:.0f}")
    clock[0] = end / 1000
    client.set_final()
    engine.market.refresh()
    with engine.lock:
        engine.storage.add_equity(clock[0], {b.name: engine.accounts[b.name].equity(engine.market.price) for b in engine.bots})
    log(f"Готово за {time.time() - t0:.0f} сек.")
    return engine, client


def print_results(engine, days):
    s = engine.summary()
    print("\n" + "═" * 92)
    print(f"  ИТОГ БЭКТЕСТА ЗА {days} ДНЕЙ  (у каждого бота было ${config.START_BALANCE:.0f})")
    print("═" * 92)
    print(f"{'Бот':<32}{'Баланс':>11}{'Итог':>9}{'Сделок':>8}{'Прибыльных':>12}{'Профит-ф.':>11}{'Просадка':>10}")
    for group, flag in (("основные", False), ("контрольные", True)):
        print(f"— {group} —")
        for b in [x for x in s["bots"] if x["control"] == flag]:
            wr = "—" if b["winrate"] is None else f"{b['winrate']:.0f}%"
            pf = "—" if b["profit_factor"] is None else f"{b['profit_factor']:.2f}"
            print(f"{b['title']:<32}{b['equity']:>10.2f}${b['pnl_pct']:>+8.1f}%{b['trades']:>8}{wr:>12}{pf:>11}{b['max_dd']:>9.1f}%")
    print("═" * 92)
    print("Профит-фактор > 1 — стратегия в плюсе. Сделок меньше 30 — результат почти случайный, смотри 90 дней.")


def main():
    ap = argparse.ArgumentParser(description="Бэктест ботов на истории Bybit")
    ap.add_argument("--days", type=int, default=30, help="сколько последних дней прогнать (1–90)")
    ap.add_argument("--fresh", action="store_true", help="скачать историю заново")
    ap.add_argument("--serve", action="store_true", help="после прогона открыть страницу с результатами")
    ap.add_argument("--port", type=int, default=8001, help="порт страницы бэктеста (по умолчанию 8001)")
    a = ap.parse_args()
    days = max(1, min(90, a.days))
    from core.bybit import BybitClient
    live = BybitClient(config.BASE_URLS, config.BYBIT_PROXY)
    try:
        live.tickers()
    except Exception as e:
        print(f"Bybit не отвечает: {e}\nЕсли сервер не видит Bybit — впиши прокси в .env (SIM_PROXY=...).")
        sys.exit(1)
    engine, client = run(live, days, a.fresh)
    print_results(engine, days)
    report = build_report(engine, days)
    name = f"data/backtest_report_{time.strftime('%Y-%m-%d')}_{days}d.md"
    with open(name, "w", encoding="utf-8") as f:
        f.write(report)
    print(f"\nОтчёт для разбора с Claude: {os.path.abspath(name)}")
    if a.serve:
        from core.dashboard import start_dashboard
        start_dashboard(engine, a.port, client, host=config.DASHBOARD_HOST, user=config.DASHBOARD_USER,
                        password=config.DASHBOARD_PASSWORD)
        shown = "IP-СЕРВЕРА" if config.DASHBOARD_HOST == "0.0.0.0" else "localhost"
        print(f"\nСтраница бэктеста: http://{shown}:{a.port}  (логин и пароль — как у основной страницы)")
        print(f"Если не открывается — открой порт: firewall-cmd --add-port={a.port}/tcp")
        print("Остановить — Ctrl+C.")
        try:
            while True:
                time.sleep(3600)
        except KeyboardInterrupt:
            pass


if __name__ == "__main__":
    main()
