"""Индикаторы для графика сделки: считаются по тем же формулам, что использует бот."""
from core.indicators import ema, rsi, macd, supertrend, stdev


def _boll(c, n=20, k=2.0):
    up, mid, lo = [None] * len(c), [None] * len(c), [None] * len(c)
    for i in range(n - 1, len(c)):
        w = c[i - n + 1:i + 1]
        m = sum(w) / n
        sd = stdev(w)
        up[i], mid[i], lo[i] = m + k * sd, m, m - k * sd
    return up, mid, lo


def _vwap(cd, n):
    out = [None] * len(cd)
    spv = sv = 0.0
    tp = [(h + l + c) / 3 for h, l, c in zip(cd.h, cd.l, cd.c)]
    for i in range(len(cd)):
        spv += tp[i] * cd.v[i]
        sv += cd.v[i]
        if i >= n:
            spv -= tp[i - n] * cd.v[i - n]
            sv -= cd.v[i - n]
        if i >= n - 1 and sv > 0:
            out[i] = spv / sv
    return out


def build(names, cd, interval_sec, anchor_ms=None):
    """Возвращает список линий: {label, color, pane: 'main' | 'ind' | 'oi', type: 'line' | 'hist', data: [...]}.
    anchor_ms — начало для VWAP от точки (начало пампа или листинга)."""
    c, out = cd.c, []

    def line(label, color, data, pane="main", width=2, kind="line"):
        out.append({"label": label, "color": color, "pane": pane, "width": width, "type": kind, "data": data})

    for nm in names:
        if nm.startswith("ema"):
            n = int(nm[3:])
            color = {"9": "#f7a600", "20": "#3b82f6", "21": "#3b82f6", "200": "#a855f7"}.get(str(n), "#94a3b8")
            line(f"EMA {n}", color, ema(c, n))
        elif nm == "donchian20":
            hi = [None] * len(c)
            lo = [None] * len(c)
            for i in range(20, len(c)):
                hi[i], lo[i] = max(cd.h[i - 20:i]), min(cd.l[i - 20:i])
            line("Максимум 20", "#22c55e", hi, width=1)
            line("Минимум 20", "#ef4444", lo, width=1)
        elif nm == "bb":
            up, mid, lo = _boll(c)
            line("Боллинджер верх", "#64748b", up, width=1)
            line("Боллинджер середина", "#f7a600", mid, width=1)
            line("Боллинджер низ", "#64748b", lo, width=1)
        elif nm == "supertrend":
            st, d = supertrend(cd.h, cd.l, c, 10, 3.0)
            line("Supertrend вверх", "#22c55e", [s if x == 1 else None for s, x in zip(st, d)])
            line("Supertrend вниз", "#ef4444", [s if x == -1 else None for s, x in zip(st, d)])
        elif nm == "avwap":
            start = next((i for i, t in enumerate(cd.t) if anchor_ms and t >= anchor_ms - 1), 0)
            data, pv, vv = [None] * len(c), 0.0, 0.0
            for i in range(start, len(c)):
                pv += (cd.h[i] + cd.l[i] + c[i]) / 3 * cd.v[i]
                vv += cd.v[i]
                data[i] = pv / vv if vv > 0 else None
            line("VWAP от начала движения", "#f7a600", data)
        elif nm == "range14":
            hi, lo = [None] * len(c), [None] * len(c)
            for i in range(14, len(c)):
                hi[i], lo[i] = max(cd.h[i - 14:i]), min(cd.l[i - 14:i])
            line("Верх 14 дней", "#64748b", hi, width=1)
            line("Низ 14 дней", "#22c55e", lo, width=1)
        elif nm == "vwap":
            line("VWAP 24ч", "#f7a600", _vwap(cd, max(2, int(86400 / interval_sec))))
        elif nm == "rsi":
            line("RSI 14", "#a855f7", rsi(c), pane="ind")
        elif nm == "macd":
            m, s = macd(c)
            line("MACD гистограмма", "#64748b", [a - b for a, b in zip(m, s)], pane="ind", kind="hist")
            line("MACD", "#3b82f6", m, pane="ind")
            line("Сигнальная", "#f7a600", s, pane="ind")
    return out
