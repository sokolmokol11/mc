"""Контрольная группа: RSI + полосы Боллинджера (возврат к среднему)."""
from bots.base import Bot, r
from core.indicators import rsi, atr, bollinger, stdev
import config


class RsiBollinger(Bot):
    name = "rsi_bb"
    title = "Охотник на скидки"
    timeframe = "15 мин"
    chart_interval = "15"
    overlays = ["bb", "rsi"]
    symbols = config.MAJORS
    description = "Покупает резкую перепроданность и шортит резкую перекупленность, ожидая, что цена вернётся к своей середине."
    rules = {
        "Индикаторы": "RSI 14 (0–100: ниже 30 — перепродано, выше 70 — перекуплено), полосы Боллинджера 20/2, ATR 14",
        "Вход в лонг": "RSI < 25 И 15-мин свеча закрылась ниже нижней полосы Боллинджера",
        "Вход в шорт": "RSI > 75 И свеча закрылась выше верхней полосы",
        "Выход": "цена вернулась к средней линии полос, или RSI вернулся к 50 (выше 55 для лонга / ниже 45 для шорта)",
        "Стоп-лосс": "2 × ATR от входа",
        "Тейк-профит": "средняя линия полос (см. выход)",
        "Лимит времени": "24 часа",
        "Размер": "12% капитала на монету (до 8 сделок)",
    }

    control = True

    def on_tick(self, ctx):
        for s in self.symbols:
            cd = ctx.market.candles(s, "15", 100)
            if not cd or len(cd) < 40 or not self.new_candle(s, cd):
                continue
            c = cd.c
            rv = rsi(c)[-1]
            mid, up, lo = bollinger(c, 20, 2)
            a = atr(cd.h, cd.l, c)[-1]
            pos = ctx.acc.positions.get(s)
            if pos and pos.side == "long" and (c[-1] >= mid or rv > 55):
                self.exit(ctx, s, f"цена вернулась к середине (RSI {rv:.0f})")
                pos = None
            elif pos and pos.side == "short" and (c[-1] <= mid or rv < 45):
                self.exit(ctx, s, f"цена вернулась к середине (RSI {rv:.0f})")
                pos = None
            if pos:
                continue
            px = ctx.market.price(s)
            d = {"Закрытие": r(c[-1]), "RSI": round(rv, 1), "Верхняя полоса": r(up), "Средняя": r(mid),
                 "Нижняя полоса": r(lo), "ATR": r(a)}
            if rv < 25 and c[-1] < lo:
                self.enter(ctx, s, s, "long", 0.12, f"перепродано: RSI {rv:.0f}, цена под нижней полосой",
                           details=d, stop=px - 2 * a, max_hold=86400)
            elif rv > 75 and c[-1] > up:
                self.enter(ctx, s, s, "short", 0.12, f"перекуплено: RSI {rv:.0f}, цена над верхней полосой",
                           details=d, stop=px + 2 * a, max_hold=86400)
