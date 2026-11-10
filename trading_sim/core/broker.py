"""Виртуальный брокер: деньги бота, открытые позиции, плечо, комиссии, стопы, ликвидация."""
from dataclasses import dataclass, field, asdict
import config

MAINTENANCE = 0.005   # поддерживающая маржа Bybit ~0.5% от размера позиции


@dataclass
class Position:
    key: str
    symbol: str
    side: str            # "long" или "short"
    qty: float           # количество монет
    entry: float         # цена входа
    margin: float        # сколько своих долларов заблокировано (залог)
    opened_at: float
    lev: float = 1.0     # плечо: размер позиции = залог × плечо
    stop: float = None
    tp: float = None
    max_hold: float = None   # секунд
    reason: str = ""
    interval: str = ""       # на каких свечах бот принимал решение (для графика)
    details: dict = field(default_factory=dict)   # значения индикаторов в момент входа
    fees: float = 0.0
    funding: float = 0.0
    first_stop: float = None
    first_tp: float = None
    market: str = "perp"     # "perp" — фьючерс, "spot" — спот (без плеча и фандинга)
    fee_rate: float = None
    extra: dict = field(default_factory=dict)

    def pnl(self, price):
        return self.qty * (price - self.entry) if self.side == "long" else self.qty * (self.entry - price)

    def liq_price(self):
        """Цена ликвидации: когда убыток съедает залог (минус поддерживающая маржа)."""
        move = (self.margin - MAINTENANCE * self.qty * self.entry) / self.qty
        return self.entry - move if self.side == "long" else self.entry + move


class Account:
    def __init__(self, bot, storage, log, label=None, lev=1.0):
        self.bot = bot
        self.label = label or bot
        self.lev = lev
        self.storage = storage
        self.log = log
        self.cash = config.START_BALANCE
        self.positions = {}
        self.on_close = None   # движок подставляет функцию, которая пишет выход в журнал решений

    def _slip(self, symbol):
        return config.SLIPPAGE_MAJOR if symbol in config.MAJORS else config.SLIPPAGE_ALT

    def equity(self, price_fn):
        total = self.cash
        for p in self.positions.values():
            px = price_fn(p.symbol) or p.entry
            total += max(0.0, p.margin + p.pnl(px))
        return total

    # ---------- сделки ----------
    def open(self, key, symbol, side, usd, price, now, stop=None, tp=None, max_hold=None,
             reason="", interval="", details=None, extra=None, market="perp"):
        """usd — залог (свои деньги). Размер позиции = usd × плечо. На споте плеча нет."""
        if key in self.positions or price is None or price <= 0:
            return None
        lev = 1.0 if market == "spot" else self.lev
        fee_rate = config.FEE_SPOT if market == "spot" else config.FEE
        usd = min(usd, self.cash / (1 + lev * fee_rate) * 0.999)
        if usd < 5:
            return None
        s = self._slip(symbol)
        fill = price * (1 + s) if side == "long" else price * (1 - s)
        notional = usd * lev
        fee = notional * fee_rate
        self.cash -= usd + fee
        pos = Position(key=key, symbol=symbol, side=side, qty=notional / fill, entry=fill, margin=usd,
                       opened_at=now, lev=lev, stop=stop, tp=tp, max_hold=max_hold, reason=reason,
                       interval=interval, details=details or {}, fees=fee, first_stop=stop, first_tp=tp,
                       market=market, fee_rate=fee_rate, extra=extra or {})
        self.positions[key] = pos
        arrow = ("🟢 СПОТ-ПОКУПКА" if market == "spot" else "🟢 ЛОНГ") if side == "long" else "🔴 ШОРТ"
        self.log(f"[{self.label}] {arrow} {symbol} залог ${usd:.0f} × {lev:g} = ${notional:.0f} "
                 f"по {fill:.6g} — {reason}")
        return pos

    def close(self, key, price, now, reason, fraction=1.0):
        pos = self.positions.get(key)
        if not pos or price is None:
            return None
        fraction = min(max(fraction, 0.0), 1.0)
        s = self._slip(pos.symbol)
        fill = price * (1 - s) if pos.side == "long" else price * (1 + s)
        q, m = pos.qty * fraction, pos.margin * fraction
        gross = q * (fill - pos.entry) if pos.side == "long" else q * (pos.entry - fill)
        exit_fee = q * fill * (pos.fee_rate if pos.fee_rate is not None else config.FEE)
        self.cash += max(0.0, m + gross) - exit_fee
        entry_fee, fund = pos.fees * fraction, pos.funding * fraction
        net = max(gross, -m) - entry_fee - exit_fee + fund
        self.storage.add_trade(
            bot=self.bot, symbol=pos.symbol, side=pos.side, qty=q, entry=pos.entry, exit=fill,
            pnl=net, pnl_pct=net / m * 100 if m else 0, fees=entry_fee + exit_fee, funding=fund,
            opened_at=pos.opened_at, closed_at=now, reason_open=pos.reason, reason_close=reason,
            lev=pos.lev, margin=m, stop=pos.first_stop, tp=pos.first_tp, last_stop=pos.stop,
            interval=pos.interval, details=pos.details, market=pos.market)
        if fraction >= 0.999:
            del self.positions[key]
        else:
            pos.qty -= q
            pos.margin -= m
            pos.fees -= entry_fee
            pos.funding -= fund
        if self.on_close:
            self.on_close(pos, net, reason, fraction, fill)
        icon = "✅" if net >= 0 else "❌"
        self.log(f"[{self.label}] {icon} закрыл {pos.symbol} {'' if fraction >= 0.999 else f'{fraction:.0%} '}"
                 f"{net:+.2f}$ ({net / m * 100 if m else 0:+.1f}% от залога) — {reason}")
        return net

    def apply_funding(self, symbol, rate, price):
        """Фандинг: при положительной ставке лонги платят шортам, при отрицательной — наоборот."""
        for p in self.positions.values():
            if p.symbol != symbol or p.market == "spot":
                continue
            amt = p.qty * price * rate
            if p.side == "long":
                amt = -amt
            self.cash += amt
            p.funding += amt

    def check_exits(self, price_fn, now):
        """Ликвидация, стоп-лосс, тейк-профит, лимит времени. Проверяется каждые TICK_SECONDS."""
        for key, p in list(self.positions.items()):
            px = price_fn(p.symbol)
            if px is None:
                continue
            reason = None
            if p.margin + p.pnl(px) <= MAINTENANCE * p.qty * px:
                reason = f"ЛИКВИДАЦИЯ (плечо {p.lev:g}x)"
            elif p.stop is not None and ((p.side == "long" and px <= p.stop) or (p.side == "short" and px >= p.stop)):
                reason = "стоп-лосс"
            elif p.tp is not None and ((p.side == "long" and px >= p.tp) or (p.side == "short" and px <= p.tp)):
                reason = "тейк-профит"
            elif p.max_hold and now - p.opened_at >= p.max_hold:
                reason = "вышло время сделки"
            elif now - p.opened_at >= config.MAX_HOLD_DAYS * 86400:
                reason = "сделка дольше месяца"
            if reason:
                self.close(key, px, now, reason)

    # ---------- сохранение ----------
    def to_dict(self):
        return {"cash": self.cash, "positions": [asdict(p) for p in self.positions.values()]}

    def load(self, d):
        self.cash = d.get("cash", config.START_BALANCE)
        known = Position.__dataclass_fields__
        self.positions = {p["key"]: Position(**{k: v for k, v in p.items() if k in known})
                          for p in d.get("positions", [])}
