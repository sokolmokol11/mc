"""Отчёт в Markdown: итоги ботов, сделки с причинами, частые причины пропусков. Удобно прислать в чат для разбора."""
from datetime import datetime

import config


def _t(ts):
    return datetime.fromtimestamp(ts).strftime("%d.%m %H:%M")


def _n(x, d=2, suffix=""):
    return "—" if x is None else f"{x:,.{d}f}{suffix}".replace(",", " ")


def _det(details):
    return "; ".join(f"{k}: {v}" for k, v in (details or {}).items() if not str(k).startswith("_"))


def build_report(engine, days=7, bot=None):
    now = engine.now()
    since = now - days * 86400
    st = engine.storage
    summary = engine.summary()
    stats = st.bot_stats(since)
    dd = st.max_drawdown(since)
    titles = {b.name: b.title for b in engine.bots}
    L = [f"# {'Бэктест на истории Bybit' if engine.mode else 'Отчёт крипто-симулятора'} за {days} дн."
         + (f" ({engine.mode})" if engine.mode else ""),
         f"Период: {_t(since)} — {_t(now)}. Стартовый баланс каждого бота ${config.START_BALANCE:.0f}. "
         f"Комиссия {config.FEE * 100:.3f}% (фьючерсы) / {config.FEE_SPOT * 100:.1f}% (спот), проскальзывание "
         f"{config.SLIPPAGE_MAJOR * 100:.2f}% / {config.SLIPPAGE_ALT * 100:.2f}%.", "",
         "## Итоги по ботам", "",
         "| Бот | Группа | Плечо | Баланс | С начала | Сделок за период | Прибыльных | Профит-фактор | Ср. прибыль | Ср. убыток | Ср. длительность | Макс. просадка |",
         "|---|---|---|---|---|---|---|---|---|---|---|---|"]
    for b in summary["bots"]:
        if bot and b["name"] != bot:
            continue
        s = stats.get(b["name"], {})
        L.append(f"| {b['title']} | {'контроль' if b['control'] else 'основной'} | {b['lev']:g}x | ${_n(b['equity'])} | "
                 f"{b['pnl_pct']:+.2f}% | {s.get('trades', 0)} | {_n(s.get('winrate'), 0, '%')} | {_n(s.get('profit_factor'))} | "
                 f"{_n(s.get('avg_win'), 2, '$')} | {_n(s.get('avg_loss'), 2, '$')} | {_n(s.get('avg_hold_h'), 1, ' ч')} | "
                 f"{_n(dd.get(b['name']), 1, '%')} |")
    L += ["", "Профит-фактор = сумма прибылей / сумма убытков (больше 1 — стратегия в плюсе). "
              "Частичные закрытия одной позиции считаются одной сделкой.", ""]

    L += ["## Открытые позиции", ""]
    any_open = False
    for b in summary["bots"]:
        if bot and b["name"] != bot:
            continue
        for p in b["positions"]:
            any_open = True
            stop_txt = "нет" if p["stop"] is None else f"{p['stop']:.6g}"
            L.append(f"- **{b['title']}**: {'лонг' if p['side'] == 'long' else 'шорт'} {p['symbol']} с {_t(p['opened_at'])}, "
                     f"вход {p['entry']:.6g}, сейчас {p['price']:.6g}, {p['pnl']:+.2f}$ ({p['pnl_pct']:+.1f}% от залога), "
                     f"стоп {stop_txt}. "
                     f"Причина входа: {p['reason_open']}. Метрики: {_det(p['details'])}")
    if not any_open:
        L.append("нет")
    L.append("")

    L += ["## Закрытые сделки", ""]
    trades = st.trades(500, bot, since)
    if not trades:
        L.append("за период сделок нет")
    for t in reversed(trades):
        L.append(f"- {_t(t['closed_at'])} · **{titles.get(t['bot'], t['bot'])}** · {'лонг' if t['side'] == 'long' else 'шорт'} "
                 f"{t['symbol']} · {t['entry']:.6g} → {t['exit']:.6g} · **{t['pnl']:+.2f}$ ({t['pnl_pct']:+.1f}%)** · "
                 f"длилась {(t['closed_at'] - t['opened_at']) / 3600:.1f} ч")
        L.append(f"  - Вход: {t['reason_open']}")
        if t.get("details"):
            L.append(f"  - Метрики при входе: {_det(t['details'])}")
        L.append(f"  - Выход: {t['reason_close']}")
    L.append("")

    L += ["## Почему боты пропускали монеты (за период)", ""]
    reasons = st.decision_reasons(since)
    for name, items in reasons.items():
        if bot and name != bot:
            continue
        L.append(f"**{titles.get(name, name)}**")
        for reason, n in items[:12]:
            L.append(f"- {n}× {reason}")
        L.append("")
    if not reasons:
        L.append("пропусков не записано")
    return "\n".join(L)
