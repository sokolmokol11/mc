"""Шортист пампов, версия 2 — по итогам первого бэктеста (11.10–10.11.2026).

Что изменилось по сравнению с v1:
- список наблюдения: монета, выросшая на 30%+, остаётся кандидатом 24 часа (v1 терял её, как только памп ломался);
- обязательный слом: закрытие 15-мин свечи ниже VWAP пампа (v1 мог шортить летящую ракету);
- пик не старше 12 часов;
- стоп над локальным максимумом и над VWAP (а не над пиком) — ближе, значит при том же риске позиция больше;
- выход: 1/3 на 1,5R, стоп в безубыток, остаток до цели — 30% пути от цены до пампа к пику;
- вход только если до цели минимум 1,3R (прибыль хотя бы в 1,3 раза больше риска).
"""
from bots.base import Bot, r, anchored_vwap
from bots.core_bots import pct, money
from core.indicators import rsi
import config


class PumpShortV2(Bot):
    name = "pump_short_v2"
    title = "Шортист пампов v2"
    timeframe = "15 мин, весь рынок"
    chart_interval = "15"
    overlays = ["avwap", "rsi", "oi"]
    MIN_PUMP, MIN_TURNOVER, MAX_SHORTS, RISK = 0.30, 2e6, 5, 0.015
    WATCH_HOURS, MAX_PEAK_AGE_H, MAX_STOP, MIN_RR = 24, 12, 0.25, 1.3
    description = ("Монета выросла на 30%+ — бот следит за ней сутки. Шортит, когда памп сломался: свеча закрылась ниже "
                   "VWAP пампа, и есть 2 из 3 признаков выдоха (объём, открытый интерес, фандинг). Стоп над локальным "
                   "максимумом, треть фиксирует на 1,5R, остаток держит до отката почти всего пампа.")
    rules = {
        "Список наблюдения": "любая монета, кроме 8 крупных и стейблкоинов, с ростом ≥ +30% за 24 ч и оборотом ≥ $2 млн; "
                             "остаётся под наблюдением 24 часа, даже если рост за сутки потом уменьшился",
        "Памп": "от минимума до пика ≥ +30%; пик не старше {age} часов и был хотя бы 30 минут назад",
        "VWAP пампа": "средняя цена с учётом объёма от начала пампа (минимум за сутки до пика) — средняя цена входа толпы",
        "Обязательно": "15-минутная свеча закрылась НИЖЕ VWAP пампа — памп сломан",
        "Признаки выдоха (2 из 3)": "1) объём последних 3 свечей < 60% объёма у пика; 2) открытый интерес упал ≥ 10% от пика "
                                     "за сутки; 3) фандинг ≥ +0,01% (лонги ещё платят)",
        "Не входить": "фандинг ≤ −0,05% (шортов уже много); до стопа больше {stop}%; до цели меньше {rr} риска",
        "Стоп-лосс": "на 3% выше максимума последних 2 часов или VWAP пампа — что выше",
        "Цель": "цена до пампа + 30% высоты пампа (то есть откат 70% пампа)",
        "Выход": "1/3 позиции на 1,5R (R — расстояние до стопа), стоп → безубыток, остаток до цели",
        "Аварийный выход": "фандинг ≤ −0,1% — шорты в толпе, риск сквиза",
        "Размер": "риск 1,5% капитала на сделку, до 5 шортов, одна монета — не чаще раза в 6 часов",
        "Лимит": "72 часа",
    }

    def __init__(self):
        super().__init__()
        fmt = {"age": f"{self.MAX_PEAK_AGE_H:g}", "stop": f"{self.MAX_STOP * 100:g}", "rr": f"{self.MIN_RR:g}".replace(".", ",")}
        self.rules = {k: v.format(**fmt) for k, v in self.rules.items()}

    def on_tick(self, ctx):
        self._manage(ctx)
        watch = self.state.setdefault("watch", {})
        for t in ctx.market.tickers.values():
            s = t["symbol"]
            if t["pct24h"] >= self.MIN_PUMP and t["turnover24h"] >= self.MIN_TURNOVER and s not in config.MAJORS \
                    and not ctx.market.is_stable(s) and s not in watch:
                watch[s] = ctx.now
        for s in [s for s, ts in watch.items() if ctx.now - ts > self.WATCH_HOURS * 3600]:
            del watch[s]
        for s in sorted(watch, key=lambda x: watch[x]):
            t = ctx.market.tickers.get(s)
            if not t or s in ctx.acc.positions:
                continue
            if self.on_cooldown(ctx, s, 6):
                self.decide(ctx, s, "skip", "пауза 6 ч после прошлого входа в эту монету", code="cool")
                continue
            cd = ctx.market.candles(s, "15", 200)
            if not cd or len(cd) < 60 or not self.new_candle(s, cd):
                continue
            ev = self._evaluate(ctx, s, cd, t)
            if ev["ok"] and self.count(ctx, "short") >= self.MAX_SHORTS:
                ev.update(ok=False, reason="сигнал есть, но уже открыто 5 шортов", code="max")
            if not ev["ok"]:
                self.decide(ctx, s, "skip", ev["reason"], ev["d"], code=ev["code"])
                continue
            px = ctx.market.price(s)
            risk_dist = ev["stop"] - px
            pos = self.enter_risk(ctx, s, s, "short", self.RISK, ev["stop"], ev["reason"], ev["d"],
                                  tp=ev["target"], max_hold=72 * 3600,
                                  extra={"tp1": px - 1.5 * risk_dist, "took": False})
            if pos:
                self.set_cooldown(ctx, s)

    def _evaluate(self, ctx, s, cd, t):
        h, l, c, v = cd.h, cd.l, cd.c, cd.v
        n = len(c)
        w = min(96, n)
        peak_i = max(range(n - w, n), key=lambda i: h[i])
        peak, age = h[peak_i], n - 1 - peak_i
        a0 = max(0, peak_i - 96)
        start_i = min(range(a0, peak_i + 1), key=lambda i: l[i])
        pre = l[start_i]
        pump = peak / pre - 1 if pre else 0
        avwap = anchored_vwap(cd, start_i)
        px = ctx.market.price(s) or c[-1]
        target = pre + 0.30 * (peak - pre)
        stop = max(max(h[-8:]), avwap) * 1.03
        risk_dist, reward = stop - px, px - target
        rr = reward / risk_dist if risk_dist > 0 else 0
        rr_list = [x for x in rsi(c)[-16:] if x is not None]
        d = {
            "Памп": f"{pct(pump, 0)} (от {r(pre)} до {r(peak)})",
            "Пик": f"{age * 15} мин назад",
            "Рост за 24ч": pct(t["pct24h"], 0),
            "Оборот 24ч": money(t["turnover24h"]),
            "Цена": r(px),
            "VWAP пампа": r(avwap),
            "Цена от VWAP": pct(c[-1] / avwap - 1),
            "Цена от пика": pct(px / peak - 1),
            "Фандинг": f"{t['funding'] * 100:.3f}%",
            "RSI макс → сейчас": f"{max(rr_list):.0f} → {rr_list[-1]:.0f}" if rr_list else "—",
            "Стоп": f"{r(stop)} (+{stop / px - 1:.1%})",
            "Цель": f"{r(target)} ({pct(target / px - 1)})",
            "Прибыль / риск": f"{rr:.1f}",
            "_anchor": cd.t[start_i] / 1000,
            "_target": target,
            "_stop": stop,
            "_px": px,
        }
        res = {"ok": False, "d": d}
        if pump < self.MIN_PUMP:
            return {**res, "code": "small", "reason": "памп от минимума до пика меньше 30%"}
        if age < 2:
            return {**res, "code": "fresh", "reason": "пик свежий (меньше 30 мин) — памп может продолжиться"}
        if age > self.MAX_PEAK_AGE_H * 4:
            return {**res, "code": "old", "reason": f"пик старше {self.MAX_PEAK_AGE_H:g} часов — это уже не памп"}
        if c[-1] >= avwap:
            return {**res, "code": "above", "reason": "цена выше VWAP пампа — памп ещё не сломан"}
        if t["funding"] <= -0.0005:
            return {**res, "code": "neg_fund", "reason": "фандинг ≤ −0,05% — шортов уже много, риск сквиза"}

        have, miss = [], []
        vol_now = sum(v[-3:]) / 3
        around = v[max(0, peak_i - 2):peak_i + 2]
        vol_peak = sum(around) / len(around) if around else 0
        if vol_peak:
            d["Объём сейчас / у пика"] = f"{vol_now / vol_peak:.0%}"
        (have if vol_peak and vol_now < 0.6 * vol_peak else miss).append("объём упал")
        oi = ctx.market.open_interest(s, "15min", 96)
        if len(oi) >= 8:
            oi_peak = max(x[1] for x in oi)
            drop = oi[-1][1] / oi_peak - 1 if oi_peak else 0
            d["OI от пика за сутки"] = pct(drop)
            (have if drop <= -0.10 else miss).append("OI падает")
        else:
            d["OI от пика за сутки"] = "нет данных"
            miss.append("OI падает")
        (have if t["funding"] >= 0.0001 else miss).append("лонги платят фандинг")
        d["Признаков выдоха"] = f"{len(have)} из 3"
        if len(have) < 2:
            return {**res, "code": "sig:" + ",".join(have),
                    "reason": f"памп сломан, но признаков выдоха {len(have)} из 3 (нужно 2). Нет: {', '.join(miss)}"}
        if risk_dist <= 0 or stop / px - 1 > self.MAX_STOP:
            return {**res, "code": "far", "reason": f"стоп слишком далеко (больше {self.MAX_STOP * 100:g}%) — монета слишком дёргается"}
        if rr < self.MIN_RR:
            return {**res, "code": "rr", "reason": f"до цели меньше {self.MIN_RR:g} риска — поздно, большая часть падения уже прошла"}
        return {**res, "ok": True, "stop": stop, "target": target, "code": "ok",
                "reason": f"памп {pct(pump, 0)} сломан (цена ниже VWAP), " + ", ".join(have) + f"; прибыль/риск {rr:.1f}"}

    def _manage(self, ctx):
        for key, p in list(ctx.acc.positions.items()):
            px = ctx.market.price(p.symbol)
            if px is None:
                continue
            t = ctx.market.tickers.get(p.symbol)
            if t and t["funding"] <= -0.001:
                self.exit(ctx, key, f"фандинг {t['funding'] * 100:.3f}% — шорты в толпе, риск сквиза")
                continue
            if not p.extra.get("took") and px <= p.extra.get("tp1", 0):
                ctx.acc.close(key, px, ctx.now, "тейк 1/3 на 1,5R", fraction=1 / 3)
                p.extra["took"] = True
                p.stop = p.entry


class PumpShortV21(PumpShortV2):
    """v2.1 — по итогам бэктеста 12.08–10.11.2026 (86 сделок v2):
    сделки с пиком старше 9 ч дали −$101, со стопом дальше 8% (самые дёрганые монеты) — −$128."""
    name = "pump_short_v21"
    title = "Шортист пампов v2.1"
    MAX_PEAK_AGE_H, MAX_STOP = 9, 0.08
    description = PumpShortV2.description + " Версия 2.1: только пики не старше 9 часов и стоп не дальше 8% — бешеные монеты пропускает."
