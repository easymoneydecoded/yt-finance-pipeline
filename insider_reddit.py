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

SUBREDDITS = ["StockMarket", "ValueInvesting", "stocks"]
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


def build_post(start: date, end: date, week: list[dict]) -> tuple[str, str]:
    by = defaultdict(list)
    for b in week:
        by[b["ticker"]].append(b)
    clusters = sorted(
        ((t, bs) for t, bs in by.items() if len({b["owner_cik"] for b in bs}) >= 2),
        key=lambda x: (-len({b["owner_cik"] for b in x[1]}), -sum(b["value"] for b in x[1])))[:8]
    people = sorted((b for b in merge_by_person(week) if is_person(b)), key=lambda b: -b["value"])[:8]
    span = f"{start:%b} {start.day}–{end:%b} {end.day}" if start.month != end.month else f"{start:%b} {start.day}–{end.day}"

    title = (f"Insider buying this week ({span}): {len(clusters)} stocks where several insiders bought"
             if clusters else f"Insider buying this week ({span}): the biggest purchases by CEOs and directors")
    lines = [f"Every week I go through all SEC Form 4 filings and pull out the open-market purchases "
             f"(transaction code P: insiders paying cash at market price, no option exercises or grants).",
             "",
             f"This week: {len(week)} insider buys worth {ia.money(sum(b['value'] for b in week))} "
             f"across {len(by)} stocks.", ""]
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
            stake = "new position" if b["stake_change"] is None else (
                ">+999%" if b["stake_change"] > 999 else f"+{b['stake_change']:.0f}%")
            lines.append(f"| ${b['ticker']} | {site.person(b)} | {site.short(b['role'], 30)} | "
                         f"{ia.money(b['value'])} | ${b['price']:,.2f} | {stake} |")
        lines.append("")
    plans = sum(b["plan"] for b in week)
    if plans:
        lines += [f"{plans} of this week's buys were pre-scheduled under 10b5-1 plans, which are usually a weaker signal.", ""]
    lines += ["Insiders sell for many reasons, but they usually buy for one. Curious which of these you'd look into "
              "and which look like noise.", "",
              "Data straight from SEC EDGAR. I keep the full list by stock at tickerpress.net. Not financial advice."]
    return title, "\n".join(lines)


def main(preview: bool = False) -> None:
    start, end, week = week_data()
    if not week:
        print("Sin compras esta semana")
        return
    title, body = build_post(start, end, week)
    sub = SUBREDDITS[end.isocalendar().week % len(SUBREDDITS)]
    url = f"https://www.reddit.com/r/{sub}/submit?" + urlencode({"selftext": "true", "title": title, "text": body})
    if len(url) > MAX_URL:
        url = f"https://www.reddit.com/r/{sub}/submit?" + urlencode({"selftext": "true", "title": title})
    print(f"r/{sub}\n{title}\n\n{body}\n\n({len(url)} caracteres de URL)")
    if preview:
        return

    msg = (f"📝 <b>Post de Reddit de la semana</b> → r/{sub}\n"
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


if __name__ == "__main__":
    main(preview=sys.argv[1:2] == ["preview"])
