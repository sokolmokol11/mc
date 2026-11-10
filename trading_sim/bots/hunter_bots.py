"""Контрольная группа: Ловец сквизов (покупка после обвала при перегретых шортах)."""
from bots.base import Bot, r
from core.indicators import rsi, atr, ema
import config


class SqueezeLong(Bot):
    name = "squeeze_long"
    title = "Ловец сквизов"
    timeframe = "15 мин, весь рынок"
    chart_interval = "15"
    overlays = ["ema20"]
    MAX_LONGS = 3
    description = "Зеркало шортиста: после обвала на монете слишком много шортов. Когда цена разворачивается, шортисты вынуждены откупать — бот едет на этом выкупе."
    rules = {
        "Индикаторы": "падение за 24ч, фандинг, оборот, дно за сутки, EMA 20, минимумы свечей (15 мин)",
        "Отбор": "любая монета, кроме 8 крупных: падение ≥ 15% за сутки, фандинг ≤ −0.05% (шорты платят лонгам), оборот ≥ $3 млн",
        "Вход в лонг": "цена закрылась выше EMA 20 И минимум последней свечи выше минимума 5 предыдущих (более высокий минимум) "
                       "И цена ещё не ушла от дна больше чем на 15%",
        "Вход в шорт": "не шортит",
        "Стоп-лосс": "на 3% ниже дна за сутки",
        "Тейк-профит": "+15% от входа",
        "Лимит времени": "48 часов",
        "Размер": "15% капитала на сделку, до 3 сделок; на одну монету — не чаще раза в 12 часов",
    }

    control = True

    def on_tick(self, ctx):
        if self.count(ctx) >= self.MAX_LONGS:
            return
        cands = [t for t in ctx.market.tickers.values()
                 if t["pct24h"] <= -0.15 and t["funding"] <= -0.0005 and t["turnover24h"] >= 3e6
                 and t["symbol"] not in config.MAJORS]
        cands.sort(key=lambda t: t["funding"])
        for t in cands[:8]:
            s = t["symbol"]
            if s in ctx.acc.positions or self.on_cooldown(ctx, s, 12):
                continue
            cd = ctx.market.candles(s, "15", 120)
            if not cd or len(cd) < 40 or not self.new_candle(s, cd):
                continue
            l, c = cd.l, cd.c
            bottom = min(l[-96:])
            e20 = ema(c, 20)[-1]
            higher_low = l[-1] > min(l[-6:-1]) and min(l[-6:-1]) <= bottom * 1.02
            if c[-1] > e20 and higher_low and c[-1] < bottom * 1.15:
                px = ctx.market.price(s)
                if self.enter(ctx, s, s, "long", 0.15,
                              f"обвал {t['pct24h']:+.0%}, фандинг {t['funding'] * 100:.2f}%, разворот вверх",
                              details={"Падение за 24ч": f"{t['pct24h']:+.0%}", "Фандинг": f"{t['funding'] * 100:.3f}%",
                                       "Дно за сутки": r(bottom), "Цена от дна": f"{c[-1] / bottom - 1:+.1%}",
                                       "Закрытие": r(c[-1]), "EMA 20": r(e20)},
                              stop=bottom * 0.97, tp=px * 1.15, max_hold=48 * 3600):
                    self.set_cooldown(ctx, s)
                if self.count(ctx) >= self.MAX_LONGS:
                    return
