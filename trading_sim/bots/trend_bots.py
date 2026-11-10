"""Контрольная группа: Supertrend (следование за трендом по индикатору)."""
from bots.base import Bot, r
from core.indicators import ema, atr, macd, supertrend, crossed_up, crossed_down
import config


class SuperTrend(Bot):
    name = "supertrend"
    title = "Супертренд"
    timeframe = "30 мин"
    chart_interval = "30"
    overlays = ["supertrend"]
    symbols = config.MAJORS[:5]
    description = "Индикатор Supertrend рисует линию под ценой в росте и над ценой в падении. Бот входит, когда линия перепрыгивает, и едет за ней."
    rules = {
        "Индикаторы": "Supertrend (ATR 10, множитель 3)",
        "Вход в лонг": "линия Supertrend перешла под цену (стала зелёной) на закрытии 30-мин свечи",
        "Вход в шорт": "линия перешла над ценой (стала красной)",
        "Выход": "линия перепрыгнула обратно, или цена коснулась стопа",
        "Стоп-лосс": "на линии Supertrend; после каждой свечи подтягивается за линией (трейлинг-стоп)",
        "Тейк-профит": "нет",
        "Размер": "18% капитала на монету (до 5 сделок)",
    }

    control = True

    def on_tick(self, ctx):
        for s in self.symbols:
            cd = ctx.market.candles(s, "30", 200)
            if not cd or len(cd) < 50 or not self.new_candle(s, cd):
                continue
            line, d = supertrend(cd.h, cd.l, cd.c, 10, 3.0)
            pos = ctx.acc.positions.get(s)
            if pos:
                if pos.side == "long" and d[-1] == 1:
                    pos.stop = max(pos.stop or 0, line[-1])
                elif pos.side == "short" and d[-1] == -1:
                    pos.stop = min(pos.stop or 1e18, line[-1])
                else:
                    self.exit(ctx, s, "Supertrend развернулся")
                    pos = None
            if pos or d[-2] is None or d[-1] == d[-2]:
                continue
            side = "long" if d[-1] == 1 else "short"
            self.enter(ctx, s, s, side, 0.18, f"Supertrend стал {'зелёным' if side == 'long' else 'красным'}",
                       details={"Закрытие": r(cd.c[-1]), "Линия Supertrend": r(line[-1]),
                                "Было": "вниз" if side == "long" else "вверх",
                                "Стало": "вверх" if side == "long" else "вниз"},
                       stop=line[-1])
