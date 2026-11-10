"""Запуск симулятора. На Windows — run.bat, на сервере — install_server.sh (запускает как службу)."""
import os
import sys

os.chdir(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.getcwd())
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

import config
from bots import ALL_BOTS
from core.bybit import BybitClient
from core.dashboard import start_dashboard
from core.engine import Engine
from core.storage import Storage


def main():
    print("=" * 64)
    print("  КРИПТО-СИМУЛЯТОР: боты торгуют виртуальными деньгами на реальных ценах Bybit")
    print("=" * 64, flush=True)
    storage = Storage(config.DB_PATH)
    engine = Engine(BybitClient(config.BASE_URLS, config.BYBIT_PROXY), storage, ALL_BOTS)
    start_dashboard(engine, config.DASHBOARD_PORT, BybitClient(config.BASE_URLS, config.BYBIT_PROXY),
                    host=config.DASHBOARD_HOST, user=config.DASHBOARD_USER, password=config.DASHBOARD_PASSWORD)
    shown = "IP-СЕРВЕРА" if config.DASHBOARD_HOST == "0.0.0.0" else "localhost"
    print(f"Страница с результатами: http://{shown}:{config.DASHBOARD_PORT}\n", flush=True)
    if not config.SERVER_MODE:
        try:
            import webbrowser
            webbrowser.open(f"http://localhost:{config.DASHBOARD_PORT}")
        except Exception:
            pass
    try:
        engine.run_forever()
    except KeyboardInterrupt:
        print("\nОстановлено. Всё сохранено — при следующем запуске боты продолжат с того же места.")


if __name__ == "__main__":
    main()
