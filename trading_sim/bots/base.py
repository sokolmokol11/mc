"""Базовый класс бота. Чтобы сделать нового бота — скопируй любой класс из папки bots и поменяй логику в on_tick."""


class Bot:
    name = "base"          # короткое имя латиницей (используется в базе и в config.LEVERAGE)
    title = "Бот"          # имя для людей
    timeframe = ""         # на каких свечах смотрит (текстом)
    description = ""       # идея в одном-двух предложениях
    rules = {}             # подробные правила: {"Вход в лонг": "...", "Стоп-лосс": "...", ...}
    chart_interval = "15"  # свечи для графика сделки
    overlays = []          # индикаторы на графике сделки (см. core/overlays.py)
    symbols = None         # список монет или None = весь рынок
    control = False        # True = контрольная группа (для сравнения)

    def __init__(self):
        self.state = {}

    def on_tick(self, ctx):
        """Вызывается каждые TICK_SECONDS. ctx.market — цены и свечи, ctx.acc — счёт, ctx.now — время (сек)."""
        raise NotImplementedError

    def symbols_text(self):
        if not self.symbols:
            return "весь рынок фьючерсов Bybit"
        return ", ".join(s.replace("USDT", "") for s in self.symbols)

    # ---------- журнал решений ----------
    def decide(self, ctx, symbol, action, reason, details=None, code=None, interval=None):
        """Записать решение. action: enter / skip / exit / info.
        Пропуски пишутся только когда причина поменялась (code), чтобы журнал не забивался одинаковыми строками."""
        if action == "skip":
            last = self.state.setdefault("last_dec", {})
            code = code or reason
            if last.get(symbol) == code:
                return
            last[symbol] = code
        else:
            self.state.setdefault("last_dec", {}).pop(symbol, None)
        ctx.decide(symbol, action, reason, details, interval)

    # ---------- помощники ----------
    def new_candle(self, key, candles):
        """True один раз на каждую новую закрытую свечу (чтобы не торговать одну свечу дважды)."""
        t = candles.t[-1]
        k = "last_" + key
        if self.state.get(k) == t:
            return False
        self.state[k] = t
        return True

    def equity(self, ctx):
        return ctx.acc.equity(ctx.market.price)

    def stake(self, ctx, frac):
        """Залог для сделки: доля от текущего капитала."""
        return self.equity(ctx) * frac

    def enter(self, ctx, key, symbol, side, frac, reason, details=None, **kw):
        """Открыть сделку на долю капитала. details — значения метрик, из-за которых бот вошёл."""
        price = ctx.market.price(symbol)
        pos = ctx.acc.open(key, symbol, side, self.stake(ctx, frac), price, ctx.now, reason=reason,
                           interval=self.chart_interval, details=details, **kw)
        if pos:
            self.decide(ctx, symbol, "enter", reason, pos.details)
        return pos

    def enter_risk(self, ctx, key, symbol, side, risk, stop, reason, details=None, max_margin_frac=0.3, **kw):
        """Открыть сделку так, чтобы при срабатывании стопа потерять ровно risk от капитала.
        Размер позиции = капитал × risk / расстояние до стопа."""
        price = ctx.market.price(symbol)
        if not price or not stop:
            return None
        dist = abs(price - stop) / price
        if dist <= 0:
            return None
        eq = self.equity(ctx)
        notional = eq * risk / dist
        lev = 1.0 if kw.get("market") == "spot" else ctx.acc.lev
        margin = min(notional / lev, eq * max_margin_frac)
        details = dict(details or {})
        details["Риск при стопе"] = f"{risk:.1%} капитала (${eq * risk:.0f})"
        details["До стопа"] = f"{dist:.1%}"
        pos = ctx.acc.open(key, symbol, side, margin, price, ctx.now, stop=stop, reason=reason,
                           interval=self.chart_interval, details=details, **kw)
        if pos:
            self.decide(ctx, symbol, "enter", reason, pos.details)
        return pos

    def exit(self, ctx, key, reason, fraction=1.0):
        pos = ctx.acc.positions.get(key)
        if pos:
            return ctx.acc.close(key, ctx.market.price(pos.symbol), ctx.now, reason, fraction)

    def count(self, ctx, side=None):
        return sum(1 for p in ctx.acc.positions.values() if side is None or p.side == side)

    def on_cooldown(self, ctx, symbol, hours):
        return ctx.now - self.state.get("cool", {}).get(symbol, 0) < hours * 3600

    def set_cooldown(self, ctx, symbol):
        self.state.setdefault("cool", {})[symbol] = ctx.now


def r(x):
    """Округление для показа на странице."""
    if x is None:
        return None
    a = abs(x)
    return round(x, 2 if a >= 100 else 4 if a >= 1 else 8)


def anchored_vwap(cd, start):
    """VWAP (средняя цена с учётом объёма) от свечи start до конца."""
    pv = v = 0.0
    for i in range(start, len(cd)):
        tp = (cd.h[i] + cd.l[i] + cd.c[i]) / 3
        pv += tp * cd.v[i]
        v += cd.v[i]
    return pv / v if v > 0 else cd.c[-1]
