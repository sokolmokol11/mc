"""Четыре основные стратегии (подробный разбор — в документе «Стратегии для крипто-симулятора»).

Каждый бот пишет в журнал решений не только входы и выходы, но и почему он ПРОПУСТИЛ монету-кандидата,
со значениями всех метрик. Ключи details, начинающиеся с "_", — служебные (для графика), на странице не показываются.
"""
from statistics import median

from bots.base import Bot, r, anchored_vwap
from core.indicators import rsi, sma
import config

DAY = 86400


def pct(x, digits=1):
    return f"{x:+.{digits}%}"


def money(x):
    return f"${x / 1e6:.1f} млн"


# =====================================================================================
class PumpShort(Bot):
    name = "pump_short"
    title = "Шортист пампов"
    timeframe = "15 мин, весь рынок"
    chart_interval = "15"
    overlays = ["avwap", "rsi", "oi"]
    MIN_PUMP, MIN_TURNOVER, MAX_SHORTS, RISK, MAX_STOP = 0.40, 3e6, 4, 0.015, 0.25
    description = ("Мелкая монета выросла на 40%+ за сутки. Бот ждёт, пока памп выдохнется (падает объём и открытый интерес, "
                   "лонги переплачивают фандинг, цена ломается под VWAP пампа), и шортит. Стоп над пиком, риск 1,5% капитала.")
    rules = {
        "Отбор": "любая монета, кроме 8 крупных и стейблкоинов: рост ≥ +40% за 24 ч, оборот ≥ $3 млн; смотрит 10 самых выросших",
        "Обязательно": "пик был ≥ 30 мин назад; цена ушла от пика на 5–25%; фандинг не отрицательный (иначе шортов уже много — риск сквиза)",
        "Признаки выдоха (нужно 3 из 4)": "1) объём последних 3 свечей < 60% объёма у пика; 2) открытый интерес (OI) упал ≥ 10% от пика за сутки; "
                                          "3) фандинг ≥ +0,03% (лонги переплачивают); 4) более низкий максимум И закрытие ниже VWAP пампа",
        "VWAP пампа": "средняя цена с учётом объёма от начала пампа (самая низкая точка за сутки до пика) — средняя цена входа толпы",
        "Стоп-лосс": "на 3% выше пика; если до стопа больше 25% — пропуск",
        "Размер": "риск 1,5% капитала: позиция = капитал × 1,5% / расстояние до стопа (не больше 30% капитала в залоге)",
        "Выход": "половина на −15% (стоп остатка → в безубыток), остаток — трейлинг-стоп 10% над минимумом или тейк на цене до пампа",
        "Аварийный выход": "фандинг ушёл ниже −0,05% — шорты в толпе, риск сквиза",
        "Лимит": "72 часа; до 4 шортов одновременно; одну монету — не чаще раза в 12 часов",
    }

    def on_tick(self, ctx):
        self._manage(ctx)
        cands = [t for t in ctx.market.tickers.values()
                 if t["pct24h"] >= self.MIN_PUMP and t["turnover24h"] >= self.MIN_TURNOVER
                 and t["symbol"] not in config.MAJORS and not ctx.market.is_stable(t["symbol"])]
        cands.sort(key=lambda t: -t["pct24h"])
        for t in cands[:10]:
            s = t["symbol"]
            if s in ctx.acc.positions:
                continue
            if self.on_cooldown(ctx, s, 12):
                self.decide(ctx, s, "skip", "пауза 12 ч после прошлого входа в эту монету", code="cool")
                continue
            cd = ctx.market.candles(s, "15", 200)
            if not cd or len(cd) < 60 or not self.new_candle(s, cd):
                continue
            ev = self._evaluate(ctx, s, cd, t)
            if ev["ok"] and self.count(ctx, "short") >= self.MAX_SHORTS:
                ev.update(ok=False, reason="сигнал есть, но уже открыто 4 шорта", code="max")
            if not ev["ok"]:
                self.decide(ctx, s, "skip", ev["reason"], ev["d"], code=ev["code"])
                continue
            px = ctx.market.price(s)
            pre = ev["pre"]
            final_tp = pre if pre < px * 0.85 else px * 0.70
            pos = self.enter_risk(ctx, s, s, "short", self.RISK, ev["stop"], ev["reason"], ev["d"],
                                  max_hold=72 * 3600,
                                  extra={"tp1": px * 0.85, "took": False, "low": px, "final_tp": final_tp})
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
        avwap = anchored_vwap(cd, start_i)
        retr = c[-1] / peak - 1
        rr = [x for x in rsi(c)[-16:] if x is not None]
        d = {
            "Рост за 24ч": pct(t["pct24h"], 0),
            "Оборот 24ч": money(t["turnover24h"]),
            "Пик": f"{r(peak)} ({age * 15} мин назад)",
            "Цена сейчас": r(c[-1]),
            "Цена от пика": pct(retr),
            "Цена до пампа": r(pre),
            "VWAP пампа": r(avwap),
            "Фандинг": f"{t['funding'] * 100:.3f}%",
            "RSI макс → сейчас": f"{max(rr):.0f} → {rr[-1]:.0f}" if rr else "—",
            "_anchor": cd.t[start_i] / 1000,
        }
        res = {"ok": False, "d": d, "pre": pre}
        if age < 2:
            return {**res, "code": "fresh", "reason": "пик свежий (меньше 30 мин) — памп может продолжиться"}
        if retr > -0.05:
            return {**res, "code": "near", "reason": "цена ещё у пика (откат меньше 5%)"}
        if retr < -0.25:
            return {**res, "code": "late", "reason": "опоздали: цена уже больше чем на 25% ниже пика"}
        if t["funding"] < 0:
            return {**res, "code": "neg_fund", "reason": "фандинг отрицательный — шортов уже много, риск сквиза"}

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

        (have if t["funding"] >= 0.0003 else miss).append("лонги переплачивают фандинг")

        lower_high = max(h[-3:]) < peak * 0.98
        below = c[-1] < avwap
        d["Структура"] = ("более низкий максимум" if lower_high else "максимум не ниже пика") + \
                         (", закрытие ниже VWAP пампа" if below else ", закрытие выше VWAP пампа")
        (have if lower_high and below else miss).append("слом структуры")

        d["Признаков выдоха"] = f"{len(have)} из 4"
        code = "sig:" + ",".join(have)
        if len(have) < 3:
            return {**res, "code": code,
                    "reason": f"признаков выдоха {len(have)} из 4 (нужно 3). Есть: {', '.join(have) or '—'}. "
                              f"Нет: {', '.join(miss)}"}
        stop = peak * 1.03
        dist = stop / c[-1] - 1
        if dist > self.MAX_STOP:
            return {**res, "code": "far", "reason": "стоп слишком далеко (больше 25% от цены)"}
        return {**res, "ok": True, "stop": stop, "code": "ok",
                "reason": f"памп {pct(t['pct24h'], 0)} выдыхается: " + ", ".join(have)}

    def _manage(self, ctx):
        for key, p in list(ctx.acc.positions.items()):
            px = ctx.market.price(p.symbol)
            if px is None:
                continue
            t = ctx.market.tickers.get(p.symbol)
            ex = p.extra
            ex["low"] = min(ex.get("low", px), px)
            if t and t["funding"] <= -0.0005:
                self.exit(ctx, key, f"фандинг {t['funding'] * 100:.3f}% — шорты в толпе, риск сквиза")
                continue
            if not ex.get("took") and px <= ex.get("tp1", 0):
                ctx.acc.close(key, px, ctx.now, "тейк 50% на −15%", fraction=0.5)
                ex["took"] = True
                p.stop = p.entry
                p.tp = ex.get("final_tp", p.entry * 0.70)
            if ex.get("took"):
                p.stop = min(p.stop, ex["low"] * 1.10)


# =====================================================================================
class ListingShort(Bot):
    name = "listing_short"
    title = "Шортист листингов"
    timeframe = "4 часа, новые фьючерсы"
    chart_interval = "240"
    overlays = ["avwap"]
    RISK, MAX_SHORTS, MAX_STOP = 0.01, 5, 0.35
    description = ("Новые токены в среднем теряют 20–25% за первый месяц: фонды и команда продают дешёвые токены. "
                   "Бот шортит фьючерсы возрастом 1–14 дней, когда цена ниже средней цены с момента листинга и отскок к ней провалился.")
    rules = {
        "Отбор": "фьючерс запущен на Bybit 1–14 дней назад, оборот ≥ $5 млн за 24 ч, не стейблкоин",
        "VWAP листинга": "средняя цена с учётом объёма от первой свечи фьючерса — средняя цена, по которой все купили",
        "Вход в шорт": "4-часовая свеча закрылась ниже VWAP листинга, И за последние 3 свечи цена подходила к VWAP (ближе 3%), "
                       "но ни одна свеча не закрылась выше — отскок провалился",
        "Фильтры": "фандинг ≥ −0,03%; BTC за 7 дней вырос не больше чем на 10%",
        "Стоп-лосс": "на 5% выше максимума провалившегося отскока (или VWAP, если он выше): цена вернулась выше VWAP — идея сломана; "
                     "если до стопа больше 35% — пропуск",
        "Размер": "риск 1% капитала, до 5 шортов одновременно",
        "Выход": "половина на −15% (стоп остатка → в безубыток), остаток на −25%",
        "Лимит": "21 день",
    }

    def on_tick(self, ctx):
        self._manage(ctx)
        if ctx.now - self.state.get("scan", 0) < 300:
            return
        self.state["scan"] = ctx.now
        btc = ctx.market.candles("BTCUSDT", "D", 15)
        btc7 = btc.c[-1] / btc.c[-8] - 1 if btc and len(btc) >= 8 else 0.0
        for s, lt in ctx.market.launch_times().items():
            if not lt or s in config.MAJORS or ctx.market.is_stable(s) or s in ctx.acc.positions:
                continue
            age = (ctx.now * 1000 - lt) / 86_400_000
            if not 1 <= age <= 14:
                continue
            t = ctx.market.tickers.get(s)
            if not t:
                continue
            d = {"Возраст фьючерса": f"{age:.1f} дн", "Оборот 24ч": money(t["turnover24h"]),
                 "Фандинг": f"{t['funding'] * 100:.3f}%", "BTC за 7 дней": pct(btc7), "_anchor": lt / 1000}
            if t["turnover24h"] < 5e6:
                self.decide(ctx, s, "skip", "оборот меньше $5 млн за сутки", d, code="turnover")
                continue
            cd = ctx.market.candles(s, "240", 100)
            if not cd or len(cd) < 6:
                self.decide(ctx, s, "skip", "мало истории (меньше 6 свечей по 4 часа)", d, code="young")
                continue
            if not self.new_candle(s, cd):
                continue
            start = next((i for i, x in enumerate(cd.t) if x >= lt - 4 * 3600 * 1000), 0)
            vwap = anchored_vwap(cd, start)
            bounce_hi = max(max(cd.h[-3:]), vwap)
            touched = max(cd.h[-3:]) >= vwap * 0.97
            all_below = all(x < vwap for x in cd.c[-3:])
            px = ctx.market.price(s)
            stop = bounce_hi * 1.05
            dist = stop / px - 1
            d.update({"Цена": r(px), "VWAP листинга": r(vwap), "Цена от VWAP": pct(px / vwap - 1),
                      "Максимум отскока": r(max(cd.h[-3:])), "Отскок к VWAP": "был, не закрылся выше" if touched and all_below
                      else ("не было" if not touched else "закрылся выше")})
            if cd.c[-1] >= vwap:
                self.decide(ctx, s, "skip", "цена выше VWAP листинга — покупатели пока сильнее", d, code="above")
            elif not (touched and all_below):
                self.decide(ctx, s, "skip", "ждём отскок к VWAP, который не сможет закрыться выше", d, code="no_bounce")
            elif t["funding"] < -0.0003:
                self.decide(ctx, s, "skip", "фандинг ниже −0,03% — шортов уже много", d, code="neg_fund")
            elif btc7 > 0.10:
                self.decide(ctx, s, "skip", "рынок в сильном росте (BTC за неделю больше +10%)", d, code="btc")
            elif self.count(ctx, "short") >= self.MAX_SHORTS:
                self.decide(ctx, s, "skip", "сигнал есть, но уже открыто 5 шортов", d, code="max")
            elif dist > self.MAX_STOP:
                self.decide(ctx, s, "skip", "стоп слишком далеко (больше 35% от цены)", d, code="far")
            else:
                self.enter_risk(ctx, s, s, "short", self.RISK, stop,
                                f"листинг {age:.0f} дн назад, цена {pct(px / vwap - 1)} от VWAP, отскок провалился",
                                d, tp=px * 0.75, max_hold=21 * DAY, extra={"tp1": px * 0.85, "took": False})

    def _manage(self, ctx):
        for key, p in list(ctx.acc.positions.items()):
            px = ctx.market.price(p.symbol)
            if px is not None and not p.extra.get("took") and px <= p.extra.get("tp1", 0):
                ctx.acc.close(key, px, ctx.now, "тейк 50% на −15%", fraction=0.5)
                p.extra["took"] = True
                p.stop = p.entry


# =====================================================================================
class RepeatPumpSpot(Bot):
    name = "repeat_pump"
    title = "Охотник за повторными пампами"
    timeframe = "1 день, спот"
    chart_interval = "D"
    overlays = ["range14"]
    MAX_POS, FRAC = 10, 0.10
    description = ("Твоя спот-схема: монеты, которые уже несколько раз резко пампились и откатывались, "
                   "покупаются на дне спокойного боковика и продаются сразу на следующем всплеске. Без плеча.")
    rules = {
        "Отбор": "фьючерс торгуется > 180 дней, оборот $1–30 млн за 24 ч, не крупная монета и не стейблкоин",
        "История пампов": "за последние 120 дней минимум 2 дня, когда максимум был на 30%+ выше вчерашнего закрытия, "
                          "а в следующие 7 дней цена вернулась (закрытие не выше +10% от уровня до пампа)",
        "Покупка": "последние 14 дней боковик (максимум не дальше 25% от минимума), цена в нижней трети диапазона",
        "Продажа": "лимит на половине типичного прошлого пампа (от +15% до +35%), или сразу, если за 24 ч +15% "
                   "при объёме в 5 раз выше среднего",
        "Стоп": "дневное закрытие ниже дна диапазона на 8%",
        "Лимит": "30 дней",
        "Размер": "10% капитала на монету, до 10 монет, спот без плеча (комиссия 0,1%)",
        "Оговорка": "цены берутся с фьючерса Bybit — они почти совпадают со спотом; есть ли монета на споте, бот не проверяет",
        "Когда": "проверка раз в сутки, после закрытия дневной свечи (03:05 по Москве)",
    }

    def on_tick(self, ctx):
        self._manage(ctx)
        day = int(ctx.now // DAY)
        if self.state.get("scan_day") == day or ctx.now % DAY < 300:
            return
        self.state["scan_day"] = day
        launches = ctx.market.launch_times()
        universe = [t for t in ctx.market.tickers.values()
                    if 1e6 <= t["turnover24h"] <= 30e6 and t["symbol"] not in config.MAJORS
                    and not ctx.market.is_stable(t["symbol"])
                    and launches.get(t["symbol"]) and (ctx.now * 1000 - launches[t["symbol"]]) / 86_400_000 > 180]
        found = 0
        for t in universe:
            s = t["symbol"]
            if s in ctx.acc.positions:
                continue
            cd = ctx.market.candles(s, "D", 130)
            if not cd or len(cd) < 121:
                continue
            h, l, c, v = cd.h, cd.l, cd.c, cd.v
            spikes = []
            for i in range(len(c) - 120, len(c) - 1):
                size = h[i] / c[i - 1] - 1
                after = c[i + 1:i + 8]
                if size >= 0.30 and after and min(after) <= c[i - 1] * 1.10:
                    spikes.append(size)
            if len(spikes) < 2:
                continue
            found += 1
            hi, lo = max(h[-14:]), min(l[-14:])
            width = hi / lo - 1
            where = (c[-1] - lo) / (hi - lo) if hi > lo else 0
            target = min(max(median(spikes) / 2, 0.15), 0.35)
            d = {"Пампов за 120 дней": f"{len(spikes)} (типичный +{median(spikes):.0%})",
                 "Боковик 14 дней": f"{r(lo)} – {r(hi)} (размах {width:.0%})",
                 "Где цена в диапазоне": f"{where:.0%} от дна", "Цель продажи": f"+{target:.0%}",
                 "Оборот 24ч": money(t["turnover24h"]), "_range_lo": lo, "_range_hi": hi}
            if width > 0.25:
                self.decide(ctx, s, "skip", "нет боковика: размах за 14 дней больше 25%", d, code="wide")
            elif where > 1 / 3:
                self.decide(ctx, s, "skip", "цена не у дна боковика (выше нижней трети)", d, code="high")
            elif self.count(ctx) >= self.MAX_POS:
                self.decide(ctx, s, "skip", "сигнал есть, но уже куплено 10 монет", d, code="max")
            else:
                px = ctx.market.price(s)
                self.enter(ctx, s, s, "long", self.FRAC,
                           f"повторный пампер ({len(spikes)} пампа за 120 дней) у дна боковика", d,
                           tp=px * (1 + target), max_hold=30 * DAY, market="spot",
                           extra={"range_lo": lo, "avg_v": sum(v[-21:-1]) / 20})
        ctx.log(f"[{self.title}] дневная проверка: монет в отборе {len(universe)}, с историей пампов {found}")

    def _manage(self, ctx):
        for key, p in list(ctx.acc.positions.items()):
            t = ctx.market.tickers.get(p.symbol)
            avg_v = p.extra.get("avg_v")
            if t and avg_v and t["pct24h"] >= 0.15 and t["volume24h"] >= 5 * avg_v:
                self.exit(ctx, key, f"всплеск {pct(t['pct24h'], 0)} за сутки на объёме {t['volume24h'] / avg_v:.0f}× — продаём в памп")
                continue
            cd = ctx.market.candles(p.symbol, "D", 5)
            if cd and self.new_candle("stop_" + key, cd) and cd.t[-1] / 1000 >= p.opened_at - DAY \
                    and cd.c[-1] < p.extra.get("range_lo", 0) * 0.92:
                self.exit(ctx, key, "дневное закрытие на 8% ниже дна боковика — диапазон сломан")


# =====================================================================================
class WeeklyMomentum(Bot):
    name = "weekly_momentum"
    title = "Недельный моментум"
    timeframe = "раз в неделю, топ-30"
    chart_interval = "240"
    overlays = ["ema20"]
    description = ("Крупные монеты, которые сильнее всех росли последние 3 недели, чаще продолжают обгонять рынок. "
                   "Каждый понедельник бот покупает 3 лидера и шортит 3 аутсайдера среди 30 самых торгуемых монет.")
    rules = {
        "Отбор": "30 фьючерсов Bybit с самым большим оборотом, без стейблкоинов, торгуются > 60 дней",
        "Когда": "каждый понедельник в 03:00 по Москве (при первом запуске — сразу)",
        "Ранжирование": "по росту цены за последние 21 день",
        "Лонг": "3 лучших по 25% капитала; если BTC ниже своей 50-дневной средней — по 12,5%",
        "Шорт": "3 худших по 8% капитала — страховка от общего падения рынка",
        "Стоп-лосс": "−12% от входа на каждую позицию",
        "Выход": "через неделю пересчёт: осталась в тройке — держим, выпала — закрываем; одна монета не дольше 4 недель",
    }

    def on_tick(self, ctx):
        week = int((ctx.now - 4 * DAY) // (7 * DAY))     # неделя с понедельника 00:00 UTC
        if self.state.get("week") == week or not ctx.market.tickers:
            return
        launches = ctx.market.launch_times()
        top = sorted((t for t in ctx.market.tickers.values()
                      if not ctx.market.is_stable(t["symbol"]) and launches.get(t["symbol"])
                      and (ctx.now * 1000 - launches[t["symbol"]]) / 86_400_000 > 60),
                     key=lambda t: -t["turnover24h"])[:30]
        ranked = []
        for t in top:
            cd = ctx.market.candles(t["symbol"], "D", 30)
            if cd and len(cd) >= 22:
                ranked.append((t["symbol"], cd.c[-1] / cd.c[-22] - 1))
        if len(ranked) < 10:
            return
        self.state["week"] = week
        ranked.sort(key=lambda x: -x[1])
        btc = ctx.market.candles("BTCUSDT", "D", 60)
        btc_sma = sma(btc.c, 50)[-1] if btc and len(btc) >= 50 else None
        risk_off = bool(btc_sma and btc.c[-1] < btc_sma)
        long_frac = 0.125 if risk_off else 0.25
        longs, shorts = [s for s, _ in ranked[:3]], [s for s, _ in ranked[-3:]]
        want = {"L:" + s for s in longs} | {"S:" + s for s in shorts}
        table = {f"{i + 1}. {s.replace('USDT', '')}": pct(x) for i, (s, x) in enumerate(ranked)}
        table["BTC к 50-дневной средней"] = "ниже — лонги уменьшены вдвое" if risk_off else "выше"
        self.decide(ctx, ranked[0][0], "info", f"недельный пересчёт: лонг {', '.join(x.replace('USDT', '') for x in longs)}; "
                                              f"шорт {', '.join(x.replace('USDT', '') for x in shorts)}", table)
        expired = set()
        for key, p in list(ctx.acc.positions.items()):
            if ctx.now - p.opened_at >= 28 * DAY - 3600:
                self.exit(ctx, key, "держали 4 недели — лимит")
                expired.add(key)
            elif key not in want:
                self.exit(ctx, key, "выпала из тройки по росту за 21 день")
        rank_of = {s: (i + 1, x) for i, (s, x) in enumerate(ranked)}
        for side, coins, frac, stop_k in (("long", longs, long_frac, 0.88), ("short", shorts, 0.08, 1.12)):
            for s in coins:
                key = ("L:" if side == "long" else "S:") + s
                if key in ctx.acc.positions or key in expired:
                    continue
                place, ret = rank_of[s]
                px = ctx.market.price(s)
                self.enter(ctx, key, s, side, frac,
                           f"№{place} из {len(ranked)} по росту за 21 день ({pct(ret)})",
                           {"Место": f"{place} из {len(ranked)}", "Рост за 21 день": pct(ret),
                            "BTC к 50-дневной": "ниже" if risk_off else "выше", "Доля капитала": f"{frac:.1%}"},
                           stop=px * stop_k, max_hold=28 * DAY)
