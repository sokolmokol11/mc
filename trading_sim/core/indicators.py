"""Технические индикаторы. Все функции возвращают список той же длины, что и вход (None — пока мало данных)."""
import math


def ema(xs, n):
    out, k, e = [], 2 / (n + 1), None
    for x in xs:
        e = x if e is None else x * k + e * (1 - k)
        out.append(e)
    return out


def sma(xs, n):
    out, s = [], 0.0
    for i, x in enumerate(xs):
        s += x
        if i >= n:
            s -= xs[i - n]
        out.append(s / n if i >= n - 1 else None)
    return out


def rsi(c, n=14):
    out = [None] * len(c)
    if len(c) <= n:
        return out
    ag = al = 0.0
    for i in range(1, n + 1):
        d = c[i] - c[i - 1]
        ag += max(d, 0)
        al += max(-d, 0)
    ag, al = ag / n, al / n
    out[n] = 100.0 if al == 0 else 100 - 100 / (1 + ag / al)
    for i in range(n + 1, len(c)):
        d = c[i] - c[i - 1]
        ag = (ag * (n - 1) + max(d, 0)) / n
        al = (al * (n - 1) + max(-d, 0)) / n
        out[i] = 100.0 if al == 0 else 100 - 100 / (1 + ag / al)
    return out


def atr(h, l, c, n=14):
    out = [None] * len(c)
    if len(c) < n:
        return out
    tr = [h[0] - l[0]] + [max(h[i] - l[i], abs(h[i] - c[i - 1]), abs(l[i] - c[i - 1])) for i in range(1, len(c))]
    a = sum(tr[:n]) / n
    out[n - 1] = a
    for i in range(n, len(c)):
        a = (a * (n - 1) + tr[i]) / n
        out[i] = a
    return out


def stdev(xs):
    if len(xs) < 2:
        return 0.0
    m = sum(xs) / len(xs)
    return math.sqrt(sum((x - m) ** 2 for x in xs) / (len(xs) - 1))


def bollinger(c, n=20, k=2.0):
    """Полосы Боллинджера для последней свечи: (середина, верх, низ)."""
    w = c[-n:]
    mid = sum(w) / len(w)
    sd = stdev(w)
    return mid, mid + k * sd, mid - k * sd


def macd(c, fast=12, slow=26, sig=9):
    ef, es = ema(c, fast), ema(c, slow)
    line = [a - b for a, b in zip(ef, es)]
    return line, ema(line, sig)


def supertrend(h, l, c, n=10, mult=3.0):
    """Возвращает (линия, направление): направление 1 — вверх, -1 — вниз."""
    a = atr(h, l, c, n)
    line, direction = [None] * len(c), [None] * len(c)
    fu = fl = None
    d = 1
    for i in range(len(c)):
        if a[i] is None:
            continue
        hl2 = (h[i] + l[i]) / 2
        bu, bl = hl2 + mult * a[i], hl2 - mult * a[i]
        if fu is None:
            fu, fl = bu, bl
        else:
            fu = bu if (bu < fu or c[i - 1] > fu) else fu
            fl = bl if (bl > fl or c[i - 1] < fl) else fl
            if d == -1 and c[i] > fu:
                d = 1
            elif d == 1 and c[i] < fl:
                d = -1
        direction[i] = d
        line[i] = fl if d == 1 else fu
    return line, direction


def crossed_up(a, b):
    return a[-2] is not None and b[-2] is not None and a[-2] <= b[-2] and a[-1] > b[-1]


def crossed_down(a, b):
    return a[-2] is not None and b[-2] is not None and a[-2] >= b[-2] and a[-1] < b[-1]
