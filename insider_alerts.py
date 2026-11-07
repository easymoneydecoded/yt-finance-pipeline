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
   python insider_alerts.py teaser   -> un aviso premium, con retraso, en el canal gratis
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


# El codigo P es "open market OR private purchase": las notas al pie dicen si fue una colocacion privada
# o una oferta (comprar al emisor, no en bolsa). Un lector de Reddit nos lo senalo con razon (RAPP).
OFFERING_RE = re.compile(r"private placement|subscription agreement|securities purchase agreement|registered direct|"
                         r"underwritten|public offering|initial public offering|\bIPO\b|\bPIPE\b|directed share program|"
                         r"purchased (?:directly )?from the (?:issuer|company)", re.I)


def is_offering(texts) -> bool:
    return any(OFFERING_RE.search(t or "") for t in texts)


def alertable(e: dict) -> bool:
    return not e.get("dup") and e["value"] >= (MIN_BUY_USD if e["kind"] == "buy" else MIN_SELL_USD)


# ---------------------------------------------------------------------------
# La misma compra declarada dos veces
# ---------------------------------------------------------------------------
# Un fondo y el consejero que lo representa (o una sociedad y su dueno) presentan cada uno su Form 4 por LAS MISMAS
# acciones: misma empresa, fecha, acciones y precio. Contarlas dos veces inflaba totales y rankings (ASPE 4-nov,
# ETRA 5-nov; 1.300 M$ en el historico). Solo se fusionan si uno de los declarantes es una entidad: dos personas
# distintas que compran lo mismo (p. ej. consejeros en una ampliacion) son compras distintas.
ENTITY_RE = re.compile(r"\b(LLC|L\.?P|LP|LLP|FUNDS?|PARTNERS|CAPITAL|ADVISORS|ADVISERS|MANAGEMENT|HOLDINGS?|INC|"
                       r"LTD|LIMITED|TRUST|GROUP|VENTURES?|CORP|CORPORATION|GMBH|AG|BV|NV|PLC|INVESTMENTS?)\b\.?", re.I)


def dup_key(b: dict) -> tuple:
    return (b.get("ticker"), b.get("trade_date"), b.get("shares"), round(float(b.get("price") or 0), 4))


def is_entity(name: str) -> bool:
    return bool(ENTITY_RE.search(name or ""))


def same_buy_groups(rows: list[dict]) -> list[list[dict]]:
    """Grupos (de 2 o mas) que son la misma compra declarada por varios, en el orden de entrada."""
    groups: dict[tuple, list[dict]] = {}
    for b in rows:
        if b.get("kind", "buy") == "buy" and b.get("shares"):
            groups.setdefault(dup_key(b), []).append(b)
    return [g for g in groups.values()
            if len({x.get("owner_cik") or x.get("owner") for x in g}) > 1 and any(is_entity(x["owner"]) for x in g)]


def mark_duplicates(events: list[dict]) -> int:
    """Alertas: el primer aviso se queda (puede que ya se enviara); los demas se marcan 'dup' y no avisan ni cuentan."""
    n = 0
    for g in same_buy_groups([e for e in events if not e.get("dup")] + [e for e in events if e.get("dup")]):
        keep = g[0]
        for other in g[1:]:
            if not other.get("dup"):
                other["dup"] = True
                n += 1
            keep["co"] = sorted(set(keep.get("co", [])) | {other["owner"]})
    return n


def merge_duplicates(rows: list[dict]) -> list[dict]:
    """Web: deja una sola fila por compra (la de la persona si la hay) y los demas como declarantes conjuntos.
    Idempotente: si la SEC vuelve a traer la fila sobrante, se fusiona igual sin contar dos veces."""
    drop = set()
    for g in same_buy_groups(rows):
        g = sorted(g, key=lambda x: (is_entity(x["owner"]), x.get("acc") or ""))
        keep = g[0]
        for other in g[1:]:
            keep["co"] = sorted(set(keep.get("co", [])) | {other["owner"]} | set(other.get("co", [])))
            if not keep.get("via") and is_entity(other["owner"]):
                keep["via"] = smart_title(other["owner"])
            drop.add(id(other))
    return [b for b in rows if id(b) not in drop]


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

    notes = {f.get("id"): "".join(f.itertext()) for f in doc.findall("footnotes/footnote")}
    totals = {}
    for tx in doc.findall("nonDerivativeTable/nonDerivativeTransaction"):
        code = _txt(tx, "transactionCoding/transactionCode")
        if code not in ("P", "S"):
            continue
        shares = _num(tx, "transactionAmounts/transactionShares/value")
        price = _num(tx, "transactionAmounts/transactionPricePerShare/value")
        if shares <= 0 or price <= 0:
            continue
        t = totals.setdefault(code, {"shares": 0.0, "value": 0.0, "after": 0.0, "dates": [], "indirect": 0.0,
                                     "via": [], "notes": []})
        t["shares"] += shares
        t["value"] += shares * price
        t["after"] = _num(tx, "postTransactionAmounts/sharesOwnedFollowingTransaction/value") or t["after"]
        t["dates"].append(_txt(tx, "transactionDate/value"))
        # Directa o indirecta (fondos, fideicomisos, familia) y las notas al pie de esta linea
        if _txt(tx, "ownershipNature/directOrIndirectOwnership/value") == "I":
            t["indirect"] += shares * price
            nature = _txt(tx, "ownershipNature/natureOfOwnership/value")
            if nature and nature not in t["via"]:
                t["via"].append(nature)
        t["notes"] += [notes.get(fn.get("id"), "") for fn in tx.iter("footnoteId")]

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
            "indirect": t["indirect"] > t["value"] / 2, "via": "; ".join(t["via"])[:90],
            "offering": is_offering(t["notes"] + [_txt(doc, "remarks")]),
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
    n = (e.get("joint") or 0) + len(e.get("co", []))
    return name + (f" (+{n} joint filers)" if n else "")


def is_new_position(e: dict) -> bool:
    """Sin acciones antes de la compra. Si el saldo final es menor que lo comprado, el Form 4
    reparte la compra entre varias cuentas (directa/indirecta) y no se sabe: no se dice nada."""
    return e["stake_change"] is None and e["after"] >= e["shares"] * 0.99


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
    elif e["kind"] == "buy" and is_new_position(e):
        lines.append(f"📈 New position: {e['after']:,.0f} shares")
    lines.append(f"📅 Trade: {nice_date(e['trade_date'])} · Filed: {nice_date(e['filed'])}")
    if e["kind"] == "buy":
        n = cluster_size(e, events)
        if n >= 2:
            lines.append(f"🔥 <b>Cluster buy:</b> {n} insiders buying ${esc(e['ticker'])} in the last {CLUSTER_DAYS} days")
    if e["plan"]:
        lines.append("🗓 Pre-scheduled trade (Rule 10b5-1 plan)")
    if e.get("indirect"):
        lines.append(f"🏦 Indirect: {esc(e.get('via') or 'through an entity, not a personal account')}")
    if e.get("offering"):
        lines.append("📑 Private placement / offering, not an open-market buy")
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
    dups = mark_duplicates(state["events"])          # antes de avisar: la misma compra no se avisa dos veces
    print(f"Avisos nuevos: {found}" + (f" ({dups} eran la misma compra declarada por otro: no se avisan)" if dups else ""))

    retry_since = (datetime.now(timezone.utc) - timedelta(hours=RETRY_HOURS)).isoformat()
    pending = [e for e in state["events"]
               if not e["posted"] and alertable(e) and e["found_at"] >= retry_since]
    for e in pending:
        if send(PREMIUM_CHAT, alert_text(e, state["events"])):
            e["posted"] = True
            print(f"   ✓ {e['kind']} {e['ticker']} {money(e['value'])}")
            time.sleep(3)
    save_state(state)


def recap_day() -> tuple[str, bool]:
    """Dia del que habla el resumen nocturno. Devuelve (dia, forzado).
    Antes de las 21:00 de Nueva York el dia de hoy aun no ha cerrado (la SEC acepta filings hasta las 22:00),
    asi que toca el de ayer: si GitHub lanza el cron de las 03:40 UTC con horas de retraso, sigue saliendo el
    dia correcto y no un dia vacio. RECAP_DAY=AAAA-MM-DD (input 'day' del workflow) fuerza un dia concreto
    para recuperar uno perdido, aunque ya conste como publicado."""
    forced = os.getenv("RECAP_DAY", "").strip()
    if forced:
        return datetime.fromisoformat(forced).date().isoformat(), True
    now = datetime.now(ET_TZ)
    day = now.date() if now.hour >= 21 else now.date() - timedelta(days=1)
    return day.isoformat(), False


def recap() -> None:
    state = load_state()
    day, forced = recap_day()
    if day in state["recaps"] and not DRY_RUN and not forced:
        print(f"El resumen de {day} ya se publico")
        return
    events = [e for e in state["events"] if e["filed"] == day and alertable(e)]
    if not [e for e in events if e["kind"] == "buy"] and datetime.fromisoformat(day).weekday() >= 5:
        print("Fin de semana sin compras: no hay resumen")
        return
    ok_free = send(FREE_CHAT, recap_text(day, events, premium=False))
    ok_premium = send(PREMIUM_CHAT, recap_text(day, events, premium=True))
    if (ok_free or ok_premium) and day not in state["recaps"]:
        state["recaps"].append(day)
    save_state(state)


TEASER_MIN_HOURS = 6     # el aviso gratis siempre llega con retraso respecto al premium
TEASER_MAX_HOURS = 72    # el lunes aun vale una compra del viernes


def teaser() -> None:
    """Un aviso premium al dia, con retraso, en el canal gratis: ensena lo que se pierden."""
    state = load_state()
    now = datetime.now(timezone.utc)
    done = set(state.setdefault("teasers", []))
    candidates = [e for e in state["events"]
                  if e["kind"] == "buy" and e["posted"] and alertable(e) and e["acc"] not in done
                  and TEASER_MIN_HOURS <= (now - datetime.fromisoformat(e["found_at"])).total_seconds() / 3600
                  <= TEASER_MAX_HOURS]
    if not candidates:
        print("No hay avisos para el canal gratis")
        return
    # Directivos antes que fondos, luego cluster buys, luego importe
    e = min(candidates, key=lambda x: (rank_key(x)[0], -cluster_size(x, state["events"]), -x["value"]))
    hours = int((now - datetime.fromisoformat(e["found_at"])).total_seconds() // 3600)
    text = (f"⏱ <b>Premium members got this alert {hours} hours ago</b>\n\n{alert_text(e, state['events'])}")
    if PREMIUM_LINK:
        text += (f"\n\n🔒 Get every insider buy the moment it's filed:\n"
                 f'👉 <a href="{esc(PREMIUM_LINK)}">Join Premium</a>')
    if send(FREE_CHAT, text):
        state["teasers"] = (state["teasers"] + [e["acc"]])[-200:]
        print(f"   ✓ aviso gratis: {e['ticker']} {money(e['value'])} ({hours} h de retraso)")
    save_state(state)


PERF_DAYS = 30   # avisos premium que entran en el balance semanal
PERF_TOP  = 5


def performance_text(events: list[dict]) -> str | None:
    """Mejores avisos de compra del premium de los ultimos 30 dias y el balance de todos (cuantos van en positivo).
    Se ensenan solo las ganadoras arriba, pero el balance cuenta todas: sin el, el mensaje daria a entender que
    los avisos suelen acertar. None si no hay ninguna ganadora que ensenar."""
    import insider_site as site
    site.load_prices(datetime.now(timezone.utc).date())
    since = (datetime.now(timezone.utc) - timedelta(days=PERF_DAYS)).isoformat()
    alerts, seen = [], set()
    for e in events:
        b = dict(e, ticker=site.clean_ticker(e["ticker"]))
        # La misma compra presentada por varios insiders (Form 4 conjuntos) cuenta una vez
        key = (b["ticker"], b["trade_date"], round(b["price"], 2), round(b["shares"]))
        if b["kind"] != "buy" or not b["posted"] or b["found_at"] < since or key in seen:
            continue
        seen.add(key)
        ret = site.since_buy(b)
        if ret is not None:
            alerts.append((ret, b))
    winners, tickers = [], set()
    for ret, b in sorted((x for x in alerts if x[0] > 0), key=lambda x: -x[0]):
        if b["ticker"] not in tickers and len(winners) < PERF_TOP:
            winners.append((ret, b))
            tickers.add(b["ticker"])
    if not winners:
        return None
    lines = [f"🏆 <b>Best insider alerts · last {PERF_DAYS} days</b>",
             "How the top Premium alerts have done since the insider bought:", ""]
    for i, (ret, e) in enumerate(winners, 1):
        role = e["role"].split(",")[0]
        lines.append(f"{i}. <b>${esc(e['ticker'])} +{ret * 100:.0f}%</b> — {esc(role)} bought {money(e['value'])} "
                     f"@ ${e['price']:,.2f} ({nice_date(e['trade_date'] or e['filed'])})")
    up = sum(1 for ret, _ in alerts if ret > 0)
    lines += ["", f"📊 Of the {len(alerts)} buy alerts sent in the last {PERF_DAYS} days, {up} are up so far.",
              "", "<i>Latest close vs. the insider's price, split-adjusted. Past performance does not predict "
              "future results. Not financial advice.</i>"]
    return "\n".join(lines)


def performance() -> None:
    """Domingo: balance de los avisos en el canal premium (una vez por semana)."""
    state = load_state()
    week = datetime.now(ET_TZ).strftime("%G-W%V")
    if week in state.setdefault("performance", []) and not DRY_RUN:
        print(f"El balance de la semana {week} ya se publico")
        return
    text = performance_text(state["events"])
    if not text:
        print("Ningun aviso en positivo con precio: no hay balance esta semana")
        return
    if send(PREMIUM_CHAT, text):
        state["performance"] = (state["performance"] + [week])[-20:]
        print(f"   ✓ balance semanal en premium ({week})")
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
    modes = {"scan": scan, "recap": recap, "teaser": teaser, "performance": performance, "check": check, "setup": setup}
    if sys.argv[1:2] and sys.argv[1] in modes:
        modes[sys.argv[1]]()
    else:
        print("Uso: python insider_alerts.py scan | recap | teaser | performance | check | setup")
