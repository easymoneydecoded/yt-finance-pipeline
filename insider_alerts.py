"""
================================================================================
 insider_alerts.py - Bot de Telegram de compras de directivos (SEC Form 4)
================================================================================

 Producto de pago, en ingles (mismo publico que el canal de YouTube):

   - Canal PREMIUM (privado, suscripcion con Telegram Stars): cada compra en
     mercado abierto (codigo P) de un directivo >= MIN_BUY_USD, casi en directo,
     y las ventas grandes (>= MIN_SELL_USD). Marca las "cluster buys" (varios
     directivos comprando la misma accion en 30 dias).
   - Canal GRATIS (publico): resumen diario con las 3 mejores compras y el
     numero total de avisos, con el enlace para suscribirse al premium.

 Sin IA a proposito: todas las cifras salen tal cual del Form 4 de la SEC, asi
 que no hay nada que inventar ni que verificar.

 Uso:
   python insider_alerts.py scan     -> lee los Form 4 nuevos y avisa en premium
   python insider_alerts.py recap    -> resumen del dia (gratis + premium)
   python insider_alerts.py check    -> comprueba que el bot puede publicar
   python insider_alerts.py setup    -> lista los canales del bot y crea el
                                        enlace de suscripcion con Stars
   DRY_RUN=true -> imprime los mensajes en vez de enviarlos

 Secrets/variables:
   TELEGRAM_ALERTS_BOT_TOKEN (o TELEGRAM_BOT_TOKEN), ALERTS_PREMIUM_CHAT
   (-100...), ALERTS_FREE_CHAT (@canal), ALERTS_PREMIUM_LINK (enlace t.me/+...),
   SEC_CONTACT (email de contacto: la SEC lo exige en el User-Agent)
   Opcionales: MIN_BUY_USD (50000), MIN_SELL_USD (10000000), STARS_PRICE (500),
   SITE_MIN_BUY_USD (10000: compras que se guardan para tickerpress.net sin avisar)

 Estado: history/insider_state.json (el workflow lo guarda en la cache de
 Actions, no en el repo, para no hacer un commit cada hora).
================================================================================
"""

import html
import json
import os
import re
import sys
import threading
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests
from dotenv import load_dotenv

load_dotenv()

BOT_TOKEN    = os.getenv("TELEGRAM_ALERTS_BOT_TOKEN") or os.getenv("TELEGRAM_BOT_TOKEN")
PREMIUM_CHAT = os.getenv("ALERTS_PREMIUM_CHAT")
FREE_CHAT    = os.getenv("ALERTS_FREE_CHAT")
PREMIUM_LINK = os.getenv("ALERTS_PREMIUM_LINK", "")
MIN_BUY_USD  = float(os.getenv("MIN_BUY_USD") or 50_000)
MIN_SELL_USD = float(os.getenv("MIN_SELL_USD") or 10_000_000)
SITE_MIN_BUY = float(os.getenv("SITE_MIN_BUY_USD") or 10_000)   # compras mas pequenas: solo para la web
STARS_PRICE  = int(os.getenv("STARS_PRICE") or 500)
DRY_RUN      = os.getenv("DRY_RUN", "false") == "true"
BRAND        = "Ticker Press"

API        = f"https://api.telegram.org/bot{BOT_TOKEN}"
SEC_UA     = {"User-Agent": f"{BRAND} Insider Alerts {os.getenv('SEC_CONTACT') or 'admin@tickerpress.invalid'}"}
FEED       = ("https://www.sec.gov/cgi-bin/browse-edgar?action=getcurrent&type=4&company=&dateb="
              "&owner=include&start={start}&count=100&output=atom")
STATE_FILE = Path(os.getenv("INSIDER_STATE") or "history/insider_state.json")

MAX_PAGES      = 40     # 40 x 100 entradas (cada Form 4 sale 2 veces: emisor y directivo)
FIRST_RUN_BACK = int(os.getenv("FIRST_RUN_HOURS") or 3)   # horas hacia atras si no hay estado (cache perdida)
SEC_PAUSE      = 0.15   # la SEC permite 10 peticiones/s
KEEP_SEEN      = 8000
KEEP_DAYS      = 45
CLUSTER_DAYS   = 30
RETRY_HOURS    = 24     # un aviso que Telegram rechazo se reintenta durante 24 h

ATOM = "{http://www.w3.org/2005/Atom}"

try:
    from zoneinfo import ZoneInfo
    ET_TZ = ZoneInfo("America/New_York")
except Exception:            # Windows sin tzdata
    ET_TZ = timezone(timedelta(hours=-5))


# ---------------------------------------------------------------------------
# Estado
# ---------------------------------------------------------------------------
def load_state() -> dict:
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"seen": [], "last_updated": None, "events": [], "recaps": []}


def save_state(state: dict) -> None:
    cutoff = (datetime.now(timezone.utc) - timedelta(days=KEEP_DAYS)).isoformat()
    state["seen"] = state["seen"][-KEEP_SEEN:]
    state["events"] = [e for e in state["events"] if e["found_at"] >= cutoff]
    state["recaps"] = state["recaps"][-60:]
    STATE_FILE.parent.mkdir(exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, indent=1), encoding="utf-8")


# ---------------------------------------------------------------------------
# SEC
# ---------------------------------------------------------------------------
_sec_lock, _sec_last = threading.Lock(), [0.0]


def _sec_throttle() -> None:
    """Como mucho una peticion cada SEC_PAUSE segundos, tambien con varios hilos."""
    with _sec_lock:
        wait = _sec_last[0] + SEC_PAUSE - time.monotonic()
        if wait > 0:
            time.sleep(wait)
        _sec_last[0] = time.monotonic()


def sec_get(url: str) -> requests.Response:
    for attempt in range(1, 4):
        _sec_throttle()
        try:
            r = requests.get(url, headers=SEC_UA, timeout=30)
        except requests.RequestException as e:
            print(f"   . SEC sin respuesta ({e}), reintento {attempt}/3")
            time.sleep(5 * attempt)
            continue
        if r.status_code in (429, 503):
            print(f"   . SEC limita ({r.status_code}), espero")
            time.sleep(10 * attempt)
            continue
        return r
    raise RuntimeError(f"SEC no responde: {url}")


def new_filings(state: dict) -> list[dict]:
    """Form 4 del feed 'latest filings' que aun no hemos visto, del mas antiguo al mas nuevo."""
    seen = set(state["seen"])
    if state.get("last_updated"):
        cutoff = datetime.fromisoformat(state["last_updated"]) - timedelta(hours=1)
    else:
        cutoff = datetime.now(timezone.utc) - timedelta(hours=FIRST_RUN_BACK)
    found, newest = {}, None

    for page in range(MAX_PAGES):
        r = sec_get(FEED.format(start=page * 100))
        r.raise_for_status()
        entries = ET.fromstring(r.content).findall(f"{ATOM}entry")
        if not entries:
            break
        reached_cutoff = False
        for e in entries:
            title = e.findtext(f"{ATOM}title", "")
            if not title.startswith("4 - "):        # fuera 4/A (enmiendas) para no duplicar
                continue
            updated = datetime.fromisoformat(e.findtext(f"{ATOM}updated"))
            newest = max(newest or updated, updated)
            if updated < cutoff:
                reached_cutoff = True
                continue
            acc = e.findtext(f"{ATOM}id", "").rsplit("=", 1)[-1]
            href = e.find(f"{ATOM}link").get("href")
            summary = e.findtext(f"{ATOM}summary", "")
            filed = re.search(r"Filed:</b>\s*(\d{4}-\d{2}-\d{2})", summary)
            if acc and acc not in seen and acc not in found:
                found[acc] = {"acc": acc, "index_url": href, "filed": filed.group(1) if filed else "",
                              "updated": updated.isoformat()}
        if reached_cutoff:
            break
    else:
        print(f"   ! llegue a {MAX_PAGES} paginas sin alcanzar la ultima ejecucion; puede faltar algun Form 4")

    if newest:
        state["last_updated"] = newest.astimezone(timezone.utc).isoformat()
    return sorted(found.values(), key=lambda f: f["updated"])


def _txt(node, path: str) -> str:
    el = node.find(path) if node is not None else None
    return (el.text or "").strip() if el is not None and el.text else ""


def _num(node, path: str) -> float:
    try:
        return float(_txt(node, path).replace(",", ""))
    except ValueError:
        return 0.0


ACRONYMS = {"Ceo", "Cfo", "Coo", "Cto", "Cio", "Cao", "Clo", "Cmo", "Cco", "Evp", "Svp", "Vp",
            "Gc", "Llc", "Lp", "Plc", "Ii", "Iii", "Iv", "Us", "Usa", "Ai", "Reit", "Nv", "Sa", "Ag"}


def smart_title(text: str) -> str:
    """'SENIOR EVP & CFO' -> 'Senior EVP & CFO' (la SEC suele escribirlo todo en mayusculas)."""
    if not text.isupper():
        return text
    words = re.split(r"(\W+)", text.title())
    return "".join(w.upper() if w in ACRONYMS else w for w in words)


def role_of(rel) -> str:
    parts = []
    title = _txt(rel, "officerTitle")
    if _txt(rel, "isOfficer") in ("1", "true"):
        parts.append(smart_title(title) if title else "Officer")
    if _txt(rel, "isDirector") in ("1", "true"):
        parts.append("Director")
    if _txt(rel, "isTenPercentOwner") in ("1", "true"):
        parts.append("10% Owner")
    if not parts and _txt(rel, "otherText"):
        parts.append(smart_title(_txt(rel, "otherText")))
    return ", ".join(parts) or "Insider"


# Errores de los propios Form 4 (precio en centimos, acciones con ceros de mas...): una
# compra con precio > 1.000 $ o valor > 50 M$ se contrasta con el mercado antes de usarla.
CHECK_PRICE, CHECK_VALUE = 1_000, 50_000_000
PRICE_CHECKS: dict[str, bool] = {}


def _market_ok(e: dict) -> bool:
    try:
        import yfinance as yf
        t = yf.Ticker(e["ticker"].replace(".", "-"))
        day = datetime.fromisoformat(e["trade_date"] or e["filed"])
        closes = t.history(start=day - timedelta(days=7), end=day + timedelta(days=4))["Close"]
        if closes.empty:
            return False                         # sin precio no se puede verificar
        ratio = e["price"] / float(closes.median())
        if not 0.33 < ratio < 3:
            return False
        cap = getattr(t.fast_info, "market_cap", None)
        return not cap or e["value"] <= cap
    except Exception as ex:
        print(f"   . no pude verificar {e['ticker']} ({type(ex).__name__})")
        return False


def plausible(e: dict) -> bool:
    if e["price"] <= CHECK_PRICE and e["value"] <= CHECK_VALUE:
        return True
    if e["acc"] not in PRICE_CHECKS:
        PRICE_CHECKS[e["acc"]] = _market_ok(e)
        if not PRICE_CHECKS[e["acc"]]:
            print(f"   . descarto {e['ticker']} {money(e['value'])} a ${e['price']:,.2f}: no cuadra con el mercado")
    return PRICE_CHECKS[e["acc"]]


def alertable(e: dict) -> bool:
    return e["value"] >= (MIN_BUY_USD if e["kind"] == "buy" else MIN_SELL_USD)


def parse_filing(filing: dict, min_buy: float = SITE_MIN_BUY) -> list[dict]:
    """Descarga el Form 4 y devuelve 0-2 eventos (compra y/o venta agregadas)."""
    txt_url = filing["index_url"].replace("-index.htm", ".txt")
    r = sec_get(txt_url)
    if not r.ok:
        print(f"   . {filing['acc']}: HTTP {r.status_code}")
        return []
    m = re.search(r"<XML>\s*(.*?)\s*</XML>", r.text, re.S)
    if not m:
        return []
    try:
        doc = ET.fromstring(m.group(1).strip().encode())
    except ET.ParseError:
        print(f"   . {filing['acc']}: XML ilegible")
        return []

    ticker = _txt(doc, "issuer/issuerTradingSymbol").upper()
    if not ticker or ticker in ("NONE", "N/A", "NA"):
        return []
    owners = doc.findall("reportingOwner")
    owner = owners[0] if owners else None
    plan = _txt(doc, "aff10b5One") in ("1", "true")

    totals = {}
    for tx in doc.findall("nonDerivativeTable/nonDerivativeTransaction"):
        code = _txt(tx, "transactionCoding/transactionCode")
        if code not in ("P", "S"):
            continue
        shares = _num(tx, "transactionAmounts/transactionShares/value")
        price = _num(tx, "transactionAmounts/transactionPricePerShare/value")
        if shares <= 0 or price <= 0:
            continue
        t = totals.setdefault(code, {"shares": 0.0, "value": 0.0, "after": 0.0, "dates": []})
        t["shares"] += shares
        t["value"] += shares * price
        t["after"] = _num(tx, "postTransactionAmounts/sharesOwnedFollowingTransaction/value") or t["after"]
        t["dates"].append(_txt(tx, "transactionDate/value"))

    events = []
    for code, t in totals.items():
        kind = "buy" if code == "P" else "sell"
        price = t["value"] / t["shares"]
        if t["value"] < (min_buy if kind == "buy" else MIN_SELL_USD):
            continue
        before = t["after"] - t["shares"] if kind == "buy" else t["after"] + t["shares"]
        events.append({
            "acc": filing["acc"], "kind": kind, "ticker": ticker,
            "issuer": smart_title(_txt(doc, "issuer/issuerName")),
            "issuer_cik": _txt(doc, "issuer/issuerCik"),
            "owner": _txt(owner, "reportingOwnerId/rptOwnerName"),
            "owner_cik": _txt(owner, "reportingOwnerId/rptOwnerCik"),
            "joint": max(0, len(owners) - 1),
            "role": role_of(owner.find("reportingOwnerRelationship")) if owner is not None else "Insider",
            "shares": t["shares"], "value": round(t["value"], 2),
            "price": price, "after": t["after"],
            "stake_change": (t["shares"] / before * 100) if before > 0 else None,
            "trade_date": min(t["dates"]) if t["dates"] else "",
            "filed": filing["filed"], "plan": plan, "url": filing["index_url"],
            "found_at": datetime.now(timezone.utc).isoformat(), "posted": False,
        })
    return [e for e in events if plausible(e)]


# ---------------------------------------------------------------------------
# Mensajes (en ingles)
# ---------------------------------------------------------------------------
def esc(text) -> str:
    return html.escape(str(text or ""), quote=False)


def money(v: float) -> str:
    for unit, div in (("B", 1e9), ("M", 1e6), ("K", 1e3)):
        if abs(v) >= div:
            return f"${v / div:.2f}{unit}".replace(".00", "")
    return f"${v:,.0f}"


def nice_date(iso: str) -> str:
    try:
        return datetime.fromisoformat(iso).strftime("%b %-d") if os.name != "nt" else \
            datetime.fromisoformat(iso).strftime("%b %d").replace(" 0", " ")
    except ValueError:
        return iso


def owner_name(e: dict) -> str:
    # La SEC escribe "APELLIDO NOMBRE": se deja tal cual pero en formato titulo
    name = smart_title(e["owner"])
    return name + (f" (+{e['joint']} joint filers)" if e["joint"] else "")


def cluster_size(e: dict, events: list[dict]) -> int:
    since = (datetime.fromisoformat(e["found_at"]) - timedelta(days=CLUSTER_DAYS)).isoformat()
    owners = {x["owner_cik"] for x in events
              if x["kind"] == "buy" and x["issuer_cik"] == e["issuer_cik"] and x["found_at"] >= since}
    return len(owners | {e["owner_cik"]})


def alert_text(e: dict, events: list[dict]) -> str:
    if e["kind"] == "buy":
        head = f"🟢 <b>INSIDER BUY · ${esc(e['ticker'])}</b>"
        verb = "Bought"
    else:
        head = f"🔴 <b>BIG INSIDER SALE · ${esc(e['ticker'])}</b>"
        verb = "Sold"
    lines = [head, esc(e["issuer"]),
             f"👤 {esc(owner_name(e))} — {esc(e['role'])}",
             f"💵 {verb} {e['shares']:,.0f} shares @ ${e['price']:,.2f} = <b>{money(e['value'])}</b>"]
    if e["kind"] == "buy" and e["stake_change"] is not None:
        lines.append(f"📈 Stake +{e['stake_change']:.0f}% → {e['after']:,.0f} shares")
    elif e["kind"] == "buy":
        lines.append(f"📈 New position: {e['after']:,.0f} shares")
    lines.append(f"📅 Trade: {nice_date(e['trade_date'])} · Filed: {nice_date(e['filed'])}")
    if e["kind"] == "buy":
        n = cluster_size(e, events)
        if n >= 2:
            lines.append(f"🔥 <b>Cluster buy:</b> {n} insiders buying ${esc(e['ticker'])} in the last {CLUSTER_DAYS} days")
    if e["plan"]:
        lines.append("🗓 Pre-scheduled trade (Rule 10b5-1 plan)")
    lines.append(f'🔗 <a href="{esc(e["url"])}">SEC filing</a>')
    return "\n".join(lines)


def rank_key(e: dict) -> tuple:
    # Primero directivos y consejeros (la senal buena); los fondos con >10% compran por otros motivos
    person = "Director" in e["role"] or e["role"] not in ("10% Owner", "Insider")
    return (not person, -e["value"])


def recap_text(day: str, events: list[dict], premium: bool) -> str:
    buys = sorted([e for e in events if e["kind"] == "buy"], key=rank_key)
    sells = [e for e in events if e["kind"] == "sell"]
    pretty = datetime.fromisoformat(day).strftime("%a, %b %d")
    if not buys:
        return (f"📊 <b>Insider buying recap · {pretty}</b>\n\n"
                f"No open-market insider buys above {money(MIN_BUY_USD)} were filed today.")
    lines = [f"📊 <b>Insider buying recap · {pretty}</b>",
             f"{len(buys)} open-market buys · {money(sum(e['value'] for e in buys))} total", ""]
    top = buys if premium else buys[:3]
    for i, e in enumerate(top[:15], 1):
        role = e["role"].split(",")[0]
        lines.append(f"{i}. <b>${esc(e['ticker'])}</b> {money(e['value'])} — {esc(owner_name(e))} ({esc(role)})")
    if premium and sells:
        lines += ["", f"🔴 {len(sells)} sales above {money(MIN_SELL_USD)}"]
    if not premium:
        rest = len(buys) - len(top)
        lines.append("")
        if rest > 0:
            lines.append(f"🔒 +{rest} more buys" + (f" and {len(sells)} big sales" if sells else "") +
                         " today. Premium members get every alert the moment it's filed.")
        if PREMIUM_LINK:
            lines.append(f'👉 <a href="{esc(PREMIUM_LINK)}">Get real-time alerts (Premium)</a>')
    lines += ["", "<i>Data: SEC Form 4 filings. Not financial advice.</i>"]
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Telegram
# ---------------------------------------------------------------------------
def tg(method: str, **data) -> dict:
    r = requests.post(f"{API}/{method}", data=data, timeout=30)
    try:
        return r.json()
    except ValueError:
        return {"ok": False, "description": r.text[:200]}


def send(chat: str | None, text: str) -> bool:
    if DRY_RUN or not chat:
        print(("--- DRY RUN ---" if DRY_RUN else "--- sin chat configurado ---") + f"\n{text}\n")
        return DRY_RUN
    for attempt in range(1, 4):
        res = tg("sendMessage", chat_id=chat, text=text, parse_mode="HTML",
                 disable_web_page_preview="true")
        if res.get("ok"):
            return True
        retry = res.get("parameters", {}).get("retry_after")
        if retry:                                   # limite de Telegram (~20 msg/min por canal)
            time.sleep(int(retry) + 1)
            continue
        print(f"   ! Telegram: {res.get('description')}")
        return False
    return False


# ---------------------------------------------------------------------------
# Modos
# ---------------------------------------------------------------------------
def scan() -> None:
    state = load_state()
    filings = new_filings(state)
    print(f"Form 4 nuevos: {len(filings)}")
    found = 0
    for f in filings:
        try:
            events = parse_filing(f)
        except Exception as e:                      # un Form 4 raro no tumba la ejecucion
            print(f"   ! {f['acc']}: {type(e).__name__}: {e}")
            events = []
        state["seen"].append(f["acc"])
        state["events"].extend(events)
        found += sum(alertable(e) for e in events)
    print(f"Avisos nuevos: {found}")

    retry_since = (datetime.now(timezone.utc) - timedelta(hours=RETRY_HOURS)).isoformat()
    pending = [e for e in state["events"]
               if not e["posted"] and alertable(e) and e["found_at"] >= retry_since]
    for e in pending:
        if send(PREMIUM_CHAT, alert_text(e, state["events"])):
            e["posted"] = True
            print(f"   ✓ {e['kind']} {e['ticker']} {money(e['value'])}")
            time.sleep(3)
    save_state(state)


def recap() -> None:
    state = load_state()
    day = (datetime.now(ET_TZ) - timedelta(hours=4)).date().isoformat()
    if day in state["recaps"] and not DRY_RUN:
        print(f"El resumen de {day} ya se publico")
        return
    events = [e for e in state["events"] if e["filed"] == day and alertable(e)]
    if not [e for e in events if e["kind"] == "buy"] and datetime.fromisoformat(day).weekday() >= 5:
        print("Fin de semana sin compras: no hay resumen")
        return
    ok_free = send(FREE_CHAT, recap_text(day, events, premium=False))
    ok_premium = send(PREMIUM_CHAT, recap_text(day, events, premium=True))
    if ok_free or ok_premium:
        state["recaps"].append(day)
    save_state(state)


def check() -> None:
    me = tg("getMe")
    if not me.get("ok"):
        raise SystemExit(f"Token del bot no valido: {me.get('description')}")
    print(f"Bot: @{me['result']['username']}")
    ok = True
    for label, chat in (("PREMIUM", PREMIUM_CHAT), ("GRATIS", FREE_CHAT)):
        if not chat:
            print(f"   ! falta el canal {label}")
            ok = False
            continue
        res = tg("getChatMember", chat_id=chat, user_id=me["result"]["id"])
        member = res.get("result", {})
        can_post = member.get("status") == "creator" or member.get("can_post_messages")
        print(f"   {'✓' if can_post else '!'} {label} {chat}: "
              f"{member.get('status') or res.get('description')}{'' if can_post else ' (no puede publicar)'}")
        ok = ok and bool(can_post)
    r = sec_get(FEED.format(start=0))
    print(f"   {'✓' if r.ok else '!'} SEC: HTTP {r.status_code}")
    sys.exit(0 if ok and r.ok else 1)


def setup() -> None:
    """Lista los canales donde esta el bot y crea el enlace de pago del premium."""
    me = tg("getMe")
    if not me.get("ok"):
        raise SystemExit(f"Token del bot no valido: {me.get('description')}")
    print(f"Bot: @{me['result']['username']}\n")
    chats = {}
    for u in tg("getUpdates", allowed_updates='["my_chat_member","channel_post"]').get("result", []):
        chat = (u.get("my_chat_member") or u.get("channel_post") or {}).get("chat")
        if chat and chat.get("type") == "channel":
            chats[chat["id"]] = chat.get("title")
    if chats:
        print("Canales donde he visto al bot (ALERTS_PREMIUM_CHAT / ALERTS_FREE_CHAT):")
        for cid, title in chats.items():
            print(f"   {cid}   {title}")
    else:
        print("No veo canales: anade el bot como administrador del canal y publica algo en el.")

    if not PREMIUM_CHAT:
        print("\nPon ALERTS_PREMIUM_CHAT en el .env y vuelve a ejecutar para crear el enlace de pago.")
        return
    res = tg("createChatSubscriptionInviteLink", chat_id=PREMIUM_CHAT, name=f"{BRAND} Premium",
             subscription_period=2592000, subscription_price=STARS_PRICE)
    if not res.get("ok"):
        raise SystemExit(f"No pude crear el enlace de suscripcion: {res.get('description')}\n"
                         "El bot necesita ser admin del canal premium con permiso 'Invitar usuarios'.")
    print(f"\nEnlace de suscripcion ({STARS_PRICE} Stars / 30 dias):\n\n   {res['result']['invite_link']}\n")
    print("Guardalo en GitHub como variable ALERTS_PREMIUM_LINK.")


if __name__ == "__main__":
    modes = {"scan": scan, "recap": recap, "check": check, "setup": setup}
    if sys.argv[1:2] and sys.argv[1] in modes:
        modes[sys.argv[1]]()
    else:
        print("Uso: python insider_alerts.py scan | recap | check | setup")
