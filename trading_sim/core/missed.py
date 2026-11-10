"""Анализ упущенных сделок (только в бэктесте): что было бы, если бы бот вошёл там, где он отказался.

Для каждого пропуска Шортиста пампов v2/v2.1 берём цену, стоп и цель, которые бот посчитал в момент решения,
и смотрим вперёд по 15-минутным свечам до 72 часов: что цена задела раньше — стоп или цель.
Результат меряем в R (R = риск сделки = расстояние до стопа): стоп = −1R, цель = +(прибыль/риск)R.
Если за одну свечу задеты и стоп, и цель, считаем, что первым сработал стоп (осторожная оценка).
Из каждого результата вычитаем комиссии и проскальзывание.
"""
import bisect
import re

import config

HOLD_MS = 72 * 3600 * 1000
COST = 2 * (config.FEE + config.SLIPPAGE_ALT)          # вход + выход, доля цены


def _category(reason):
    reason = re.sub(r"\. Нет:.*$", "", reason)
    reason = re.sub(r"\d+[.,]?\d*", "N", reason)
    return reason


def shadow(client, symbol, ts_ms, px, stop, target):
    """Результат гипотетического шорта в R или None, если данных нет."""
    se = client.series.get(symbol)
    if se is None or not (stop > px > target > 0):
        return None
    risk = stop - px
    rr = (px - target) / risk
    cost_r = COST * px / risk
    i = bisect.bisect_left(se.t, ts_ms // 900_000 * 900_000)
    end = ts_ms + HOLD_MS
    last = px
    while i < len(se.t) and se.t[i] < end:
        if se.h[i] >= stop:
            return -1 - cost_r
        if se.l[i] <= target:
            return rr - cost_r
        last = se.c[i]
        i += 1
    return (px - last) / risk - cost_r


def analyze(engine, client):
    rows = {}
    entered = []
    for b in engine.bots:
        if not b.name.startswith("pump_short_v2"):
            continue
        for d in engine.storage.decisions(10 ** 6, b.name):
            det = d["details"] or {}
            if not {"_px", "_stop", "_target"} <= det.keys():
                continue
            res = shadow(client, d["symbol"], int(d["ts"] * 1000), det["_px"], det["_stop"], det["_target"])
            if res is None:
                continue
            if d["action"] == "enter":
                entered.append((b.title, res))
            elif d["action"] == "skip":
                rows.setdefault((b.title, _category(d["reason"])), []).append(res)
    return entered, rows


def section(engine, client, risk=0.015):
    entered, rows = analyze(engine, client)
    if not entered and not rows:
        return ""
    eq_r = config.START_BALANCE * risk
    L = ["## Упущенные сделки: что было бы, если бы бот вошёл", "",
         "Для каждого отказа взяты цена, стоп и цель, которые бот посчитал в тот момент, и проверено по истории, "
         "что цена задела раньше (72 часа, стоп первым, если оба в одной свече, минус комиссии). "
         f"R — риск одной сделки (сейчас 1,5% капитала ≈ ${eq_r:.0f}). Положительный средний R у причины отказа = "
         "фильтр отрезает прибыльные сделки; отрицательный = фильтр правильно спасает от убытков.", "",
         "| Бот | Решение | Случаев | Дошло бы до цели | Средний результат | Сумма |",
         "|---|---|---|---|---|---|"]
    by_bot = {}
    for title, r in entered:
        by_bot.setdefault(title, []).append(r)
    for title, rs in by_bot.items():
        L.append(f"| {title} | **вошёл** | {len(rs)} | {sum(x > 0 for x in rs) / len(rs):.0%} | {sum(rs) / len(rs):+.2f}R | "
                 f"{sum(rs):+.1f}R (≈${sum(rs) * eq_r:+.0f}) |")
    for (title, cat), rs in sorted(rows.items(), key=lambda kv: (kv[0][0], -sum(kv[1]))):
        if len(rs) < 3:
            continue
        L.append(f"| {title} | пропуск: {cat} | {len(rs)} | {sum(x > 0 for x in rs) / len(rs):.0%} | "
                 f"{sum(rs) / len(rs):+.2f}R | {sum(rs):+.1f}R |")
    L += ["", "Оговорка: шорт «по пропуску» вошёл бы по той цене и с тем стопом, что бот посчитал при отказе; "
              "у многих пропусков (например, «цена выше VWAP») стоп оказался бы далеко, поэтому такие строки показывают "
              "направление, а не точную прибыль.", ""]
    return "\n".join(L)
