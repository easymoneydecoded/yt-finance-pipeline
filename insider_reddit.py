"""
================================================================================
 insider_reddit.py - Post semanal de Reddit, listo para publicar en un toque
================================================================================

 Reddit no deja publicar con bots (API con aprobacion) y los subreddits de
 inversion borran la autopromocion automatica. Por eso el post NO se publica
 solo: cada domingo se prepara con los datos de la semana (site_data/) y te
 llega a tu Telegram personal con un boton que abre Reddit con el titulo y el
 texto ya rellenados. Pulsar el boton + "Post" y listo.

 Cada semana va a un subreddit distinto (SUBREDDITS) para no repetir.
 Uso: python insider_reddit.py [preview]
================================================================================
"""

import html
import json
import sys
from collections import defaultdict
from datetime import date, timedelta
from urllib.parse import urlencode

import notify
import insider_alerts as ia
import insider_site as site

# r/insiderData una semana si y otra no; los grandes piden karma (r/stocks borra al instante
# los posts de cuentas nuevas, por eso no esta)
SUBREDDITS = ["insiderData", "StockMarket", "insiderData", "ValueInvesting"]
MAX_URL    = 7000     # por encima, el boton solo rellena el titulo (el texto va en el mensaje)


def week_data() -> tuple[date, date, list[dict]]:
    rows = list(site.load_buys().values())
    end = max(date.fromisoformat(b["filed"]) for b in rows)
    start = end - timedelta(days=6)
    week = [b for b in rows if start.isoformat() <= b["filed"] <= end.isoformat() and not is_spac(b)]
    return start, end, week


def is_spac(b: dict) -> bool:
    return "acquisition" in b["issuer"].lower()   # SPAC: comprar a 10 $ la unidad no dice nada


def merge_by_person(buys: list[dict]) -> list[dict]:
    """Varias compras de la misma persona en la misma accion = una fila."""
    out = {}
    for b in sorted(buys, key=lambda b: b["filed"]):
        k = (b["ticker"], b["owner_cik"])
        if k in out:
            m = out[k]
            m["value"] += b["value"]
            m["shares"] += b["shares"]
            m["price"] = m["value"] / m["shares"]
            stakes = [x for x in (m["stake_change"], b["stake_change"]) if x is not None]
            m["stake_change"] = max(stakes) if stakes else None   # aproximado: la mayor de las dos
        else:
            out[k] = dict(b)
    return list(out.values())


def is_person(b: dict) -> bool:
    return not ia.rank_key(b)[0]       # directivo o consejero, no un fondo con >10%


def week_summary(start: date, end: date, week: list[dict]):
    """(compras por accion, cluster buys, mayores compras de directivos, "Oct 24–30")."""
    by = defaultdict(list)
    for b in week:
        by[b["ticker"]].append(b)
    clusters = sorted(
        ((t, bs) for t, bs in by.items() if len({b["owner_cik"] for b in bs}) >= 2),
        key=lambda x: (-len({b["owner_cik"] for b in x[1]}), -sum(b["value"] for b in x[1])))[:8]
    people = sorted((b for b in merge_by_person(week) if is_person(b)), key=lambda b: -b["value"])[:8]
    span = f"{start:%b} {start.day}–{end:%b} {end.day}" if start.month != end.month else f"{start:%b} {start.day}–{end.day}"
    return by, clusters, people, span


def last_week_results(start: date) -> list[str]:
    """Como han ido los cluster buys de la semana anterior desde la compra (todos, suban o bajen):
    da una razon para volver cada semana. Misma rentabilidad que la web (site.since_buy)."""
    rows = list(site.load_buys().values())
    prev_start, prev_end = start - timedelta(days=7), start - timedelta(days=1)
    prev = [dict(b, ticker=site.clean_ticker(b["ticker"])) for b in rows
            if prev_start.isoformat() <= b["filed"] <= prev_end.isoformat() and not is_spac(b)]
    _, clusters, _, span = week_summary(prev_start, prev_end, prev)
    site.load_prices(start + timedelta(days=7))
    res = [(t, r) for t, bs in clusters if (r := site.total_return(bs)) is not None]
    if not res:
        return []
    up = sum(1 for _, r in res if r > 0)
    items = ", ".join(f"${t} {r * 100:+.0f}%" if abs(r) >= 0.1 else f"${t} {r * 100:+.1f}%"
                      for t, r in sorted(res, key=lambda x: -x[1]))
    return [f"How last week's cluster buys ({span}) have done since the insiders bought: {items}. "
            f"{up} of {len(res)} are up so far.", ""]


def build_post(start: date, end: date, week: list[dict]) -> tuple[str, str]:
    by, clusters, people, span = week_summary(start, end, week)

    title = (f"Insider buying this week ({span}): {len(clusters)} stocks where several insiders bought"
             if clusters else f"Insider buying this week ({span}): the biggest purchases by CEOs and directors")
    lines = [f"Every week I go through all SEC Form 4 filings and pull out the open-market purchases "
             f"(transaction code P: insiders paying cash at market price, no option exercises or grants).",
             "",
             f"This week: {len(week)} insider buys worth {ia.money(sum(b['value'] for b in week))} "
             f"across {len(by)} stocks.", ""]
    lines += last_week_results(start)
    if clusters:
        lines += ["Cluster buys (2+ different insiders buying the same stock):", "",
                  "| Ticker | Company | Insiders | Total bought | Who |", "|---|---|---|---|---|"]
        for t, bs in clusters:
            roles = sorted({b["role"].split(",")[0].strip() for b in bs})
            lines.append(f"| ${t} | {site.short(bs[0]['issuer'], 32)} | {len({b['owner_cik'] for b in bs})} | "
                         f"{ia.money(sum(b['value'] for b in bs))} | {', '.join(roles)[:60]} |")
        lines.append("")
    if people:
        lines += ["Biggest buys by officers and directors (excluding funds with 10%+ stakes):", "",
                  "| Ticker | Insider | Role | Amount | Price | Stake change |", "|---|---|---|---|---|---|"]
        for b in people:
            stake = ("new position" if ia.is_new_position(b) else "n/a") if b["stake_change"] is None else (
                ">+999%" if b["stake_change"] > 999 else f"+{b['stake_change']:.0f}%")
            lines.append(f"| ${b['ticker']} | {site.person(b)} | {site.short(b['role'], 30)} | "
                         f"{ia.money(b['value'])} | ${b['price']:,.2f} | {stake} |")
        lines.append("")
    plans = sum(b["plan"] for b in week)
    if plans:
        lines += [f"{plans} of this week's buys were pre-scheduled under 10b5-1 plans, which are usually a weaker signal.", ""]
    lines += ["Insiders sell for many reasons, but they usually buy for one. Curious which of these you'd look into "
              "and which look like noise.", "",
              "Data straight from SEC EDGAR. I keep the full list by stock (with how each buy has done since) at "
              "tickerpress.net, and I post the day's buys on a free Telegram channel, both linked in my profile. "
              "Not financial advice."]
    return title, "\n".join(lines)


def main(preview: bool = False) -> None:
    start, end, week = week_data()
    if not week:
        print("Sin compras esta semana")
        return
    title, body = build_post(start, end, week)
    send_post(SUBREDDITS[end.isocalendar().week % len(SUBREDDITS)], title, body, "Post de Reddit de la semana", preview)


# ---------------------------------------------------------------------------
# Post del miercoles: una sola compra destacada (otro formato y otro subreddit que el del domingo)
# ---------------------------------------------------------------------------
MIDWEEK_SUBS = ["ValueInvesting", "StockMarket", "insiderData"]
MIN_CAP = 2e9   # "empresa conocida": se prefieren las de 2.000 M$ o mas de capitalizacion


def market_info(ticker: str) -> tuple[float | None, float | None, float | None]:
    """(capitalizacion, ultimo cierre, maximo de 52 semanas) con yfinance; None si no hay datos."""
    try:
        import yfinance as yf
        t = yf.Ticker(ticker.replace(".", "-"))
        h = t.history(period="1y")["Close"].dropna()
        cap = getattr(t.fast_info, "market_cap", None)
        return cap, (float(h.iloc[-1]) if len(h) else None), (float(h.max()) if len(h) else None)
    except Exception:
        return None, None, None


def pick_midweek(week: list[dict]) -> tuple[dict, float | None, float | None, float | None] | None:
    """La compra de un directivo (no un fondo) mas grande de la semana, en una empresa de 2.000 M$+ si la hay."""
    cands = sorted((dict(b, ticker=site.clean_ticker(b["ticker"])) for b in merge_by_person(week) if is_person(b)),
                   key=lambda b: -b["value"])[:12]
    infos = [(b, *market_info(b["ticker"])) for b in cands if b["ticker"]]
    grandes = [x for x in infos if x[1] and x[1] >= MIN_CAP]
    return (grandes or infos or [None])[0]


def build_midweek(b: dict, cap, close, high, rows: list[dict]) -> tuple[str, str]:
    company, role = site.short(b["issuer"], 40), b["role"].split(",")[0].strip()
    tk = b["ticker"]
    same = [x for x in rows if site.clean_ticker(x["ticker"]) == tk]
    before = [x for x in same if x["filed"] < b["filed"]]
    since = min(x["filed"] for x in rows)
    first = not before
    title = f"{company} (${tk}) {role} just bought {ia.money(b['value'])} of stock on the open market"
    if first:
        title += f" — the first insider buy there since at least {date.fromisoformat(since):%B %Y}"
    lines = [f"{site.person(b)}, {b['role']} of {company}, bought {b['shares']:,.0f} shares at ${b['price']:,.2f} "
             f"(about {ia.money(b['value'])}), according to a Form 4 filed on {ia.nice_date(b['filed'])}. "
             "Open-market purchase (code P): cash at market price, not options or a grant.", "", "Some context:", ""]
    if b["stake_change"] is not None:
        lines.append(f"- It increased their holdings by {b['stake_change']:.0f}%." if b["stake_change"] <= 999
                     else "- It increased their holdings more than tenfold.")
    elif ia.is_new_position(b):
        lines.append("- It is a new position: they had no shares before.")
    if close and high:
        drop = close / high - 1
        lines.append(f"- {tk} closed at ${close:,.2f}, {abs(drop) * 100:.0f}% below its 52-week high of ${high:,.2f}."
                     if drop < -0.03 else f"- {tk} closed at ${close:,.2f}, close to its 52-week high.")
    if cap:
        lines.append(f"- Market cap: about {ia.money(cap)}.")
    others = {x["owner_cik"] for x in same if x["owner_cik"] != b["owner_cik"]
              and x["filed"] >= (date.fromisoformat(b["filed"]) - timedelta(days=90)).isoformat()}
    if others:
        lines.append(f"- {len(others)} other insider{'s' if len(others) > 1 else ''} also bought {tk} in the last 90 days.")
    lines.append(f"- First open-market insider purchase at {company} in the filings I track (since {date.fromisoformat(since):%b %Y})."
                 if first else f"- {len({x['owner_cik'] for x in before})} insider(s) bought {tk} before this, in the filings I track.")
    if b["plan"]:
        lines.append("- It was pre-scheduled under a 10b5-1 plan, which is usually a weaker signal.")
    lines += ["", f"Source: [SEC filing]({site.sec_url(b)})", "",
              "Insiders sell for many reasons but usually buy for one. Does this change how you look at the stock?", "",
              "I track every insider buy (full list by stock and a free daily Telegram channel, linked in my profile). "
              "Not financial advice."]
    return title, "\n".join(lines)


def midweek(preview: bool = False) -> None:
    start, end, week = week_data()
    pick = pick_midweek(week) if week else None
    if not pick:
        print("Sin compras de directivos esta semana")
        return
    title, body = build_midweek(*pick, list(site.load_buys().values()))
    n = end.isocalendar().week
    sunday = SUBREDDITS[n % len(SUBREDDITS)]
    sub = next(s for i in range(3) if (s := MIDWEEK_SUBS[(n + i) % 3]) != sunday)
    send_post(sub, title, body, "Post de Reddit del miércoles (compra destacada)", preview)


def send_post(sub: str, title: str, body: str, label: str, preview: bool) -> None:
    url = f"https://www.reddit.com/r/{sub}/submit?" + urlencode({"selftext": "true", "title": title, "text": body})
    if len(url) > MAX_URL:
        url = f"https://www.reddit.com/r/{sub}/submit?" + urlencode({"selftext": "true", "title": title})
    print(f"r/{sub}\n{title}\n\n{body}\n\n({len(url)} caracteres de URL)")
    if preview:
        return

    msg = (f"📝 <b>{label}</b> → r/{sub}\n"
           f"Pulsa el botón, revisa y dale a <b>Post</b>. Si el texto no aparece rellenado, cópialo de abajo.\n\n"
           f"<b>{notify.esc(title)}</b>\n\n<pre>{html.escape(body[:3300])}</pre>")
    if not notify.enabled():
        print("Telegram personal no configurado")
        return
    r = notify.requests.post(f"{notify.API}/sendMessage", timeout=30, data={
        "chat_id": notify.CHAT_ID, "text": msg, "parse_mode": "HTML", "disable_web_page_preview": "true",
        "reply_markup": json.dumps({"inline_keyboard": [[{"text": f"Abrir r/{sub} con el post", "url": url}]]})})
    print("Enviado a Telegram" if r.ok else f"Telegram: {r.text[:200]}")
    if not r.ok:
        sys.exit(1)


def telegram_weekly(preview: bool = False) -> None:
    """Resumen de la semana en los canales de Telegram (gratis con enlace al premium)."""
    start, end, week = week_data()
    if not week:
        return
    by, clusters, people, span = week_summary(start, end, week)
    esc = ia.esc
    lines = [f"📅 <b>Insider buying · week of {span}</b>",
             f"{len(week)} open-market buys · {ia.money(sum(b['value'] for b in week))} across {len(by)} stocks", ""]
    if clusters:
        lines.append("🔥 <b>Cluster buys</b> (2+ insiders buying the same stock)")
        for t, bs in clusters[:5]:
            lines.append(f"• <b>${esc(t)}</b> {esc(site.short(bs[0]['issuer'], 28))}: "
                         f"{len({b['owner_cik'] for b in bs})} insiders, {ia.money(sum(b['value'] for b in bs))}")
        lines.append("")
    if people:
        lines.append("💰 <b>Biggest buys by officers and directors</b>")
        for b in people[:5]:
            lines.append(f"• <b>${esc(b['ticker'])}</b> {ia.money(b['value'])}: {esc(site.person(b))} "
                         f"({esc(b['role'].split(',')[0].strip())})")
        lines.append("")
    lines.append(f'📊 Every buy by stock: <a href="https://tickerpress.net">tickerpress.net</a>')
    premium_text = "\n".join(lines + ["", "<i>Data: SEC Form 4 filings. Not financial advice.</i>"])
    free_lines = list(lines)
    if ia.PREMIUM_LINK:
        free_lines.append(f'🔒 Real-time alerts the moment insiders file: <a href="{esc(ia.PREMIUM_LINK)}">Join Premium</a>')
    free_text = "\n".join(free_lines + ["", "<i>Data: SEC Form 4 filings. Not financial advice.</i>"])
    if preview:
        ia.DRY_RUN = True
    ia.send(ia.FREE_CHAT, free_text)
    ia.send(ia.PREMIUM_CHAT, premium_text)


if __name__ == "__main__":
    if sys.argv[1:2] == ["weekly"]:
        telegram_weekly(preview=sys.argv[2:3] == ["preview"])
    elif sys.argv[1:2] == ["midweek"]:
        midweek(preview=sys.argv[2:3] == ["preview"])
    else:
        main(preview=sys.argv[1:2] == ["preview"])
