"""
================================================================================
 insider_site.py - Web tickerpress.net: compras de directivos por accion
================================================================================

 Web estatica (Cloudflare Pages) con los datos del bot Insider Alerts:
   /                      ultimas compras, cluster buys y mayores compras
   /stock/<ticker>/       todas las compras de directivos de una accion
   /day/<fecha>/          compras presentadas ese dia
   /cluster-buys/         acciones con varios directivos comprando (30 dias)
   /insider/<nombre>/     todas las compras de un directivo o fondo (2+ compras)
   /insiders/             los que mas han comprado en 12 meses
   /stocks/<letra>/       directorio A-Z (para que Google encuentre todo)
   /about/                metodologia y aviso legal
 Cada pagina lleva al canal gratis de Telegram (@tickerpress_insiders).

 SEO sin "contenido de relleno": solo datos de la SEC y frases calculadas con
 ellos (sin IA). Las acciones con una sola compra pequena llevan noindex para
 no llenar Google de paginas pobres (lo que hundio CarteraPasiva).

 Datos: site_data/insider_buys.json (en el repo).
   python insider_site.py backfill [trimestres]  -> historico desde los datasets
                                                   trimestrales de la SEC + los
                                                   dias que aun no tienen dataset
   python insider_site.py update                 -> anade las compras del bot
                                                   (history/insider_state.json)
   python insider_site.py build                  -> genera la web en site_dist/
================================================================================
"""

import csv
import html
import io
import json
import os
import re
import shutil
import sys
import zipfile
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import insider_alerts as ia

SITE_URL   = "https://tickerpress.net"
TELEGRAM   = "https://t.me/tickerpress_insiders"
ETSY_SHOP  = "https://www.etsy.com/shop/NeatSheetsStudioShop"
CONTACT    = "tickerpressyt@gmail.com"
# AdSense: el ID de editor ("ca-pub-...") va en la variable de GitHub ADSENSE_CLIENT. Sin el, la web no lleva nada de
# AdSense. Con el: etiqueta de verificacion en todas las paginas, ads.txt y anuncios automaticos SOLO en las fichas
# (empresa, directivo, dia); la portada, las listas, About, Tools y las paginas legales quedan sin anuncios para no
# restar altas al Telegram premium.
ADSENSE    = os.getenv("ADSENSE_CLIENT", "").strip()
TOOLS      = [  # plantillas de la tienda de Etsy que encajan con quien invierte (pagina /tools/)
    ("Net Worth Tracker", "4607296392", "Track every account, investment and debt month by month and watch your net worth grow."),
    ("Personal Finance Planner 2027", "4607213097", "Budget, expense tracker, debt payoff, savings goals and net worth in one file."),
    ("Debt Payoff Planner", "4607253469", "Snowball or avalanche: see your debt-free date and how much interest you save."),
    ("Savings Challenge & Sinking Funds", "4607292782", "52-week challenge, 100 envelope challenge and sinking funds to build your cash."),
    ("Simple Monthly Budget 2027", "4607274270", "The no-fuss budget: plan each month, log spending and see where your money goes."),
    ("Bill Tracker 2027", "4607293834", "Every bill, due date and payment in one place, so nothing slips through."),
    ("Couples Budget 2027", "4607295114", "Shared expenses split fairly, with one budget for the household."),
    ("Small Business Income & Expense Tracker", "4607297438", "Income, expenses, profit and loss for freelancers and side hustles."),
]
DATA_FILE  = Path("site_data/insider_buys.json")
CHECKS     = Path("site_data/price_checks.json")   # resultado de ia.plausible() por Form 4
PRICES     = Path("site_data/prices.json")         # ultimo cierre y splits por accion (insider_site.py prices)
DIST       = Path("site_dist")
CACHE      = Path("data/sec")
DATASETS   = "https://www.sec.gov/data-research/sec-markets-data/insider-transactions-data-sets"
DAILY_IDX  = "https://www.sec.gov/Archives/edgar/daily-index/{y}/QTR{q}/form.{d}.idx"

FIELDS = ("acc", "ticker", "issuer", "issuer_cik", "owner", "owner_cik", "joint", "role", "shares",
          "value", "price", "after", "stake_change", "trade_date", "filed", "plan",
          "indirect", "via", "offering")   # indirecta (fondo, fideicomiso, familia) y colocacion privada u oferta
INDEX_MIN_BUYS  = 2          # una accion se indexa con 2+ compras...
INDEX_MIN_VALUE = 100_000    # ...o con una compra de 100.000 $ o mas
INSIDER_MIN_BUYS    = 2          # un insider (persona o fondo) tiene pagina con 2+ compras...
INSIDER_INDEX_BUYS  = 3          # ...y se indexa con 3+ compras...
INSIDER_INDEX_VALUE = 1_000_000  # ...o con 1 millon de $ o mas en total
LATEST_DAYS     = 7
CLUSTER_DAYS    = 30


# ---------------------------------------------------------------------------
# Datos
# ---------------------------------------------------------------------------
def load_buys() -> dict[str, dict]:
    try:
        ia.PRICE_CHECKS.update(json.loads(CHECKS.read_text(encoding="utf-8")))
    except (OSError, json.JSONDecodeError):
        pass
    try:
        rows = json.loads(DATA_FILE.read_text(encoding="utf-8"))["buys"]
    except (OSError, json.JSONDecodeError, KeyError):
        return {}
    rows = ia.merge_duplicates([b for b in rows if ia.plausible(b)])   # misma compra de un fondo y su consejero
    return {b["acc"]: b for b in rows}


def save_buys(buys: dict[str, dict]) -> None:
    rows = sorted(ia.merge_duplicates(list(buys.values())), key=lambda b: (b["filed"], b["acc"]), reverse=True)
    DATA_FILE.parent.mkdir(exist_ok=True)
    CHECKS.write_text(json.dumps(ia.PRICE_CHECKS, sort_keys=True, indent=0), encoding="utf-8")
    DATA_FILE.write_text(json.dumps({"updated": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                                     "buys": rows}, separators=(",", ":")), encoding="utf-8")
    print(f"   . {len(rows)} compras en {DATA_FILE}")


def clean_issuer(name: str) -> str:
    """'Lennar Corp /New/' -> 'Lennar Corp' (sufijos de registro de la SEC)."""
    return re.sub(r"(\s*/[A-Za-z]{1,6}/?)+\s*$", "", ia.smart_title(name or "")).strip(" ,") or name


def clean_ticker(raw: str) -> str:
    t = re.split(r"[,/;\s]+", (raw or "").strip().upper())[0]
    t = t.split(":")[-1]                           # "NYSE:FLG" -> "FLG"
    return t if re.fullmatch(r"[A-Z][A-Z0-9.\-]{0,9}", t) and t not in ("NONE", "NA", "N/A") else ""


def record(e: dict) -> dict:
    r = {k: e.get(k) for k in FIELDS}
    r["ticker"] = clean_ticker(r["ticker"])
    for k in ("shares", "after"):
        r[k] = round(r[k] or 0)
    r["value"] = round(r["value"], 2)
    r["price"] = round(r["price"], 4)
    r["stake_change"] = None if r["stake_change"] is None else round(r["stake_change"], 1)
    r["indirect"], r["offering"], r["via"] = bool(r["indirect"]), bool(r["offering"]), r["via"] or ""
    return r


def _sec_date(text: str) -> str:
    try:
        return datetime.strptime(text.strip(), "%d-%b-%Y").date().isoformat()
    except ValueError:
        return ""


def _role(relationship: str, title: str, other: str) -> str:
    parts = []
    rel = relationship.split(",")
    if "Officer" in rel:
        parts.append(ia.smart_title(title) if title else "Officer")
    if "Director" in rel:
        parts.append("Director")
    if "TenPercentOwner" in rel:
        parts.append("10% Owner")
    if not parts and other:
        parts.append(ia.smart_title(other))
    return ", ".join(parts) or "Insider"


def quarter_buys(zip_path: Path) -> list[dict]:
    """Compras en mercado abierto (codigo P) de un dataset trimestral Form 3/4/5 de la SEC."""
    z = zipfile.ZipFile(zip_path)

    def rows(name):
        return csv.DictReader(io.TextIOWrapper(z.open(name), encoding="utf-8", errors="replace"),
                              delimiter="\t", quoting=csv.QUOTE_NONE)

    totals = {}
    for r in rows("NONDERIV_TRANS.tsv"):
        if r["TRANS_CODE"] != "P" or r["TRANS_ACQUIRED_DISP_CD"] not in ("A", ""):
            continue
        try:
            shares, price = float(r["TRANS_SHARES"] or 0), float(r["TRANS_PRICEPERSHARE"] or 0)
            after = float(r["SHRS_OWND_FOLWNG_TRANS"] or 0)
        except ValueError:
            continue
        if shares <= 0 or price <= 0:
            continue
        t = totals.setdefault(r["ACCESSION_NUMBER"], {"shares": 0.0, "value": 0.0, "after": 0.0, "dates": [],
                                                      "indirect": 0.0, "via": [], "fn": set()})
        t["shares"] += shares
        t["value"] += shares * price
        t["after"] = after or t["after"]
        t["dates"].append(_sec_date(r["TRANS_DATE"]))
        if r.get("DIRECT_INDIRECT_OWNERSHIP") == "I":   # a traves de un fondo, fideicomiso o familiar
            t["indirect"] += shares * price
            if r.get("NATURE_OF_OWNERSHIP") and r["NATURE_OF_OWNERSHIP"] not in t["via"]:
                t["via"].append(r["NATURE_OF_OWNERSHIP"])
        for k, v in r.items():                           # notas al pie que cita la linea (F1, F2...)
            if k.endswith("_FN") and v:
                t["fn"].update(x.strip() for x in v.split(","))
    totals = {acc: t for acc, t in totals.items() if t["value"] >= ia.SITE_MIN_BUY}  # plausible(): en load_buys
    notes = defaultdict(list)
    for r in rows("FOOTNOTES.tsv"):
        t = totals.get(r["ACCESSION_NUMBER"])
        if t and r["FOOTNOTE_ID"] in t["fn"]:
            notes[r["ACCESSION_NUMBER"]].append(r["FOOTNOTE_TXT"])

    subs = {r["ACCESSION_NUMBER"]: r for r in rows("SUBMISSION.tsv")
            if r["ACCESSION_NUMBER"] in totals and r["DOCUMENT_TYPE"] == "4"}
    owners = defaultdict(list)
    for r in rows("REPORTINGOWNER.tsv"):
        if r["ACCESSION_NUMBER"] in subs:
            owners[r["ACCESSION_NUMBER"]].append(r)

    out = []
    for acc, s in subs.items():
        ticker = clean_ticker(s["ISSUERTRADINGSYMBOL"])
        if not ticker or not owners[acc]:
            continue
        t, o = totals[acc], owners[acc][0]
        before = t["after"] - t["shares"]
        out.append(record({
            "acc": acc, "ticker": ticker, "issuer": clean_issuer(s["ISSUERNAME"]),
            "issuer_cik": s["ISSUERCIK"], "owner": o["RPTOWNERNAME"], "owner_cik": o["RPTOWNERCIK"],
            "joint": len(owners[acc]) - 1,
            "role": _role(o["RPTOWNER_RELATIONSHIP"], o["RPTOWNER_TITLE"], o["RPTOWNER_TXT"]),
            "shares": t["shares"], "value": t["value"], "price": t["value"] / t["shares"],
            "after": t["after"], "stake_change": t["shares"] / before * 100 if before > 0 else None,
            "trade_date": min(d for d in t["dates"] if d) if any(t["dates"]) else "",
            "filed": _sec_date(s["FILING_DATE"]), "plan": s["AFF10B5ONE"] in ("1", "true"),
            "indirect": t["indirect"] > t["value"] / 2, "via": "; ".join(t["via"])[:90],
            "offering": ia.is_offering(notes[acc] + [s.get("REMARKS", "")]),
        }))
    return out


def dataset_urls() -> list[str]:
    page = ia.sec_get(DATASETS).text
    return ["https://www.sec.gov" + u for u in re.findall(r'href="(/files/[^"]+_form345\.zip)"', page)]


def day_buys(day: date) -> list[dict]:
    """Compras de un dia sin dataset todavia: indice diario de EDGAR + cada Form 4."""
    q = (day.month - 1) // 3 + 1
    r = ia.sec_get(DAILY_IDX.format(y=day.year, q=q, d=day.strftime("%Y%m%d")))
    if r.status_code in (403, 404):
        return []                                  # festivo: la SEC devuelve AccessDenied
    r.raise_for_status()
    filings = {}
    for line in r.text.splitlines():
        m = re.match(r"^4\s+.*?(\d{4}-\d{2}-\d{2}|\d{8})\s+(edgar/data/(\d+)/([\d-]+)\.txt)\s*$", line)
        if m:
            acc, cik = m.group(4), m.group(3)
            filings.setdefault(acc, {"acc": acc, "filed": day.isoformat(), "index_url":
                                     f"https://www.sec.gov/Archives/edgar/data/{cik}/{acc.replace('-', '')}/{acc}-index.htm"})
    print(f"   . {day}: {len(filings)} Form 4")

    def one(f):
        try:
            return [e for e in ia.parse_filing(f) if e["kind"] == "buy"]
        except Exception as e:
            print(f"   ! {f['acc']}: {type(e).__name__}: {e}")
            return []

    with ThreadPoolExecutor(6) as pool:
        return [record({**e, "issuer": clean_issuer(e["issuer"])}) for evs in pool.map(one, filings.values()) for e in evs]


def backfill(quarters: int = 4) -> None:
    buys = load_buys()
    CACHE.mkdir(parents=True, exist_ok=True)
    urls = dataset_urls()[:quarters]
    last_q_end = None
    for url in urls:
        path = CACHE / url.rsplit("/", 1)[-1]
        if not path.exists():
            print(f"   . descargo {path.name}")
            path.write_bytes(ia.sec_get(url).content)
        rows = quarter_buys(path)
        print(f"   . {path.name}: {len(rows)} compras")
        buys.update({b["acc"]: b for b in rows})
        y, q = re.search(r"(\d{4})q(\d)", path.name).groups()
        end = date(int(y), int(q) * 3, 1) + timedelta(days=32)
        end = end.replace(day=1) - timedelta(days=1)
        last_q_end = max(last_q_end or end, end)
    save_buys(buys)

    # Dias despues del ultimo dataset: uno a uno (y se guarda cada dia por si se corta)
    have = {b["filed"] for b in buys.values()}
    day = (last_q_end or date.today()) + timedelta(days=1)
    while day < date.today():
        if day.weekday() < 5 and day.isoformat() not in have:
            buys.update({b["acc"]: b for b in day_buys(day)})
            save_buys(buys)
        day += timedelta(days=1)


def enrich() -> None:
    """Anade 'indirect', 'via' y 'offering' a las compras guardadas: los trimestres con dataset se releen de la
    cache (data/sec) y las posteriores, Form 4 a Form 4. Se usa una vez al introducir esos campos."""
    buys = load_buys()
    for path in sorted(CACHE.glob("*_form345.zip")):
        rows = {b["acc"]: b for b in quarter_buys(path)}
        n = 0
        for acc, b in rows.items():
            if acc in buys:
                buys[acc].update({k: b[k] for k in ("indirect", "via", "offering")})
                n += 1
        print(f"   . {path.name}: {n} compras actualizadas")
    pending = [b for b in buys.values() if "offering" not in b]
    print(f"   . {len(pending)} compras sin dataset: releo su Form 4")

    def one(b):
        try:
            for e in ia.parse_filing({"acc": b["acc"], "filed": b["filed"], "index_url": sec_url(b)}):
                if e["kind"] == "buy":
                    return b["acc"], {k: e[k] for k in ("indirect", "via", "offering")}
        except Exception as ex:
            print(f"   ! {b['acc']}: {type(ex).__name__}")
        return b["acc"], None

    with ThreadPoolExecutor(6) as pool:
        for acc, extra in pool.map(one, pending):
            if extra:
                buys[acc].update(extra)
    save_buys(buys)


def update() -> None:
    buys = load_buys()
    events = ia.load_state().get("events", [])
    new = [record({**e, "issuer": clean_issuer(e["issuer"])})
           for e in events if e["kind"] == "buy" and e["acc"] not in buys and ia.plausible(e)]
    buys.update({b["acc"]: b for b in new})
    print(f"   . {len(new)} compras nuevas del bot")
    save_buys(buys)


# ---------------------------------------------------------------------------
# Precios: rentabilidad desde la compra
# ---------------------------------------------------------------------------
def update_prices(batch: int = 200) -> None:
    """Ultimo cierre, splits y cierre del dia de cada compra (para contrastar el precio declarado) de cada
    accion, con yfinance. Las acciones que fallen conservan su dato anterior."""
    import yfinance as yf
    rows = load_buys().values()
    tickers = sorted({clean_ticker(b["ticker"]) for b in rows} - {""})
    days = defaultdict(set)
    for b in rows:
        days[clean_ticker(b["ticker"])].add(b["trade_date"] or b["filed"])
    start = (date.fromisoformat(min(b["filed"] for b in rows)) - timedelta(days=30)).isoformat()
    try:
        prices = json.loads(PRICES.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        prices = {}
    done = set()
    # Yahoo corta por timeout cuando se piden muchas a la vez: las que fallan se reintentan en lotes pequenos
    for size in (batch, 25):
        pending = [t for t in tickers if t not in done]
        for i in range(0, len(pending), size):
            chunk = pending[i:i + size]
            yahoo = {t.replace(".", "-"): t for t in chunk}
            try:
                d = yf.download(list(yahoo), start=start, auto_adjust=False, actions=True,
                                progress=False, threads=size > 25, group_by="column")
            except Exception as e:
                print(f"   ! precios {chunk[0]}-{chunk[-1]}: {type(e).__name__}")
                continue
            if d.empty:
                continue
            for yt, t in yahoo.items():
                try:
                    close = d["Close"][yt].dropna()
                    splits = d["Stock Splits"][yt].fillna(0)
                except KeyError:
                    continue
                if close.empty:
                    continue
                close.index = close.index.tz_localize(None) if close.index.tz else close.index
                ref = {}
                for day in days[t]:
                    before = close[:day[:10]]  # cierre de ese dia o del ultimo dia de bolsa anterior
                    if not before.empty and (date.fromisoformat(day[:10]) - before.index[-1].date()).days <= 5:
                        ref[day] = round(float(before.iloc[-1]), 4)
                prices[t] = {"close": round(float(close.iloc[-1]), 4), "date": close.index[-1].date().isoformat(),
                             "splits": [[day.date().isoformat(), float(r)] for day, r in splits[splits > 0].items()],
                             "ref": ref}
                done.add(t)
    ok = len(done)
    PRICES.write_text(json.dumps(prices, sort_keys=True, separators=(",", ":")), encoding="utf-8")
    print(f"   . precios: {ok}/{len(tickers)} acciones actualizadas ({len(prices)} en {PRICES})")


PRICE: dict[str, dict] = {}  # ticker -> {"close", "date", "splits", "ref"} (lo carga load_prices())


def load_prices(today: date) -> None:
    """Carga site_data/prices.json. Los precios de hace mas de 10 dias (la descarga fallo varias noches) no se usan."""
    try:
        fresh = (today - timedelta(days=10)).isoformat()
        PRICE.update({t: p for t, p in json.loads(PRICES.read_text(encoding="utf-8")).items() if p["date"] >= fresh})
    except (OSError, json.JSONDecodeError):
        print("   ! sin site_data/prices.json: no hay rentabilidades (python insider_site.py prices)")


def since_buy(b: dict) -> float | None:
    """Rentabilidad desde el precio de compra hasta el ultimo cierre, ajustando los splits posteriores.
    None si no hay precio, si el precio declarado no se parece al cierre de ese dia (ADRs frente a acciones
    ordinarias, conversiones, errores del Form 4) o si el resultado es absurdo."""
    p = PRICE.get(b["ticker"])
    day = b["trade_date"] or b["filed"]
    if not p or not b["price"] or p["date"] < day:
        return None
    factor = 1.0
    for d, r in p["splits"]:
        if d > day:
            factor *= r
    ref = p.get("ref", {}).get(day)  # Yahoo da los cierres pasados ya ajustados: se deshace el ajuste
    if not ref or not 0.65 < b["price"] / (ref * factor) < 1.5:
        return None
    ret = p["close"] * factor / b["price"] - 1
    return ret if -0.99 < ret < 10 else None


def total_return(bs: list[dict]) -> float | None:
    """Rentabilidad conjunta de varias compras, ponderada por el dinero invertido en cada una."""
    pairs = [(b["value"], r) for b in bs if (r := since_buy(b)) is not None]
    invested = sum(v for v, _ in pairs)
    return sum(v * r for v, r in pairs) / invested if invested else None


def pct(ret: float | None, *, bold: bool = False) -> str:
    if ret is None:
        return "—"
    txt = f"{ret * 100:+.0f}%" if abs(ret) >= 0.1 else f"{ret * 100:+.1f}%"
    return f"<span class='{'up' if ret >= 0 else 'down'}'>{'<b>' + txt + '</b>' if bold else txt}</span>"


# ---------------------------------------------------------------------------
# HTML
# ---------------------------------------------------------------------------
def esc(text) -> str:
    return html.escape(str(text or ""))


def slug(ticker: str) -> str:
    return ticker.lower().replace(".", "-")


def fmt_date(iso: str, year: bool = True) -> str:
    try:
        d = date.fromisoformat(iso)
    except ValueError:
        return iso or "—"
    return f"{d:%b} {d.day}, {d.year}" if year else f"{d:%b} {d.day}"


def sec_url(b: dict) -> str:
    return (f"https://www.sec.gov/Archives/edgar/data/{int(b['issuer_cik'])}/"
            f"{b['acc'].replace('-', '')}/{b['acc']}-index.htm")


def person(b: dict) -> str:
    n = (b.get("joint") or 0) + len(b.get("co", []))
    return ia.smart_title(b["owner"]) + (f" +{n}" if n else "")


INSIDER_URLS: dict[str, str] = {}  # owner_cik -> /insider/<slug>/ (lo rellena build())


def person_link(b: dict, n: int = 30) -> str:
    name = esc(short(person(b), n))
    url = INSIDER_URLS.get(b["owner_cik"])
    return f"<a href='{url}'>{name}</a>" if url else name


CSS = """
:root{--bg:#090d16;--bg2:#101828;--card:#131c2e;--line:#22304a;--text:#f5f7fa;--muted:#96a2b6;
--green:#00d67d;--green-soft:rgba(0,214,125,.12);--red:#ff5c6c}
*{box-sizing:border-box}html{-webkit-text-size-adjust:100%}
body{margin:0;background:var(--bg);color:var(--text);font:16px/1.55 Inter,system-ui,-apple-system,Segoe UI,Roboto,sans-serif}
a{color:var(--green);text-decoration:none}a:hover{text-decoration:underline}
.wrap{max-width:1080px;margin:0 auto;padding:0 16px}
header{border-bottom:1px solid var(--line);background:rgba(9,13,22,.9);position:sticky;top:0;z-index:5;backdrop-filter:blur(8px)}
header .wrap{display:flex;align-items:center;gap:20px;height:60px}
.logo{font:400 24px/1 Anton,Impact,sans-serif;letter-spacing:1px;color:var(--text)}.logo b{color:var(--green);font-weight:400}
nav{display:flex;gap:18px;flex:1;overflow-x:auto;white-space:nowrap}nav a{color:var(--muted);font-size:15px}nav a:hover{color:var(--text)}
.btn{display:inline-block;background:var(--green);color:#04130b;font-weight:700;padding:10px 16px;border-radius:8px;white-space:nowrap}
.btn:hover{text-decoration:none;filter:brightness(1.08)}header .btn{padding:7px 12px;font-size:14px}
h1{font:400 clamp(30px,5vw,48px)/1.1 Anton,Impact,sans-serif;letter-spacing:.5px;margin:36px 0 10px}
h2{font-size:21px;margin:40px 0 12px}.lead{color:var(--muted);font-size:18px;max-width:760px;margin:0 0 20px}
.stats{display:grid;grid-template-columns:repeat(auto-fit,minmax(160px,1fr));gap:12px;margin:24px 0}
.stat{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:14px 16px}
.stat span{display:block;color:var(--muted);font-size:13px}.stat b{font-size:22px}
.tbl{overflow-x:auto;border:1px solid var(--line);border-radius:12px;background:var(--card)}
table{border-collapse:collapse;width:100%;font-size:14.5px}
th,td{padding:10px 12px;text-align:left;border-bottom:1px solid var(--line);white-space:nowrap}
th{color:var(--muted);font-weight:600;font-size:13px;background:var(--bg2)}tr:last-child td{border-bottom:0}
td.num,th.num{text-align:right;font-variant-numeric:tabular-nums}.tk{font-weight:700}
td small{display:block;color:var(--muted);font-size:12.5px;line-height:1.3}
.up{color:var(--green)}.down{color:var(--red)}.tag{font-size:12px;color:var(--muted);border:1px solid var(--line);border-radius:6px;padding:1px 6px;margin-left:6px}
.cta{display:flex;gap:16px;align-items:center;justify-content:space-between;flex-wrap:wrap;background:var(--green-soft);
border:1px solid rgba(0,214,125,.35);border-radius:14px;padding:18px 20px;margin:28px 0}
.cta p{margin:0;max-width:640px}.cta strong{display:block;font-size:18px}
.letters{display:flex;flex-wrap:wrap;gap:8px;margin:16px 0}.letters a{border:1px solid var(--line);border-radius:8px;padding:6px 11px;background:var(--card)}
.grid{columns:4 180px;column-gap:24px}.grid a{display:block;padding:3px 0;break-inside:avoid}
.prose{max-width:760px}.prose p{color:#d5dbe5}
footer{border-top:1px solid var(--line);margin-top:60px;padding:28px 0;color:var(--muted);font-size:13.5px}
@media(max-width:640px){header nav{display:none}h2{font-size:19px}}
"""


def page(path: str, title: str, description: str, body: str, *, noindex: bool = False, ads: bool = False) -> None:
    canonical = f"{SITE_URL}/{path}" if path else f"{SITE_URL}/"
    robots = '<meta name="robots" content="noindex,follow">' if noindex else ""
    if ADSENSE:
        robots += f'\n<meta name="google-adsense-account" content="{esc(ADSENSE)}">'
        if ads:
            robots += ('\n<script async src="https://pagead2.googlesyndication.com/pagead/js/adsbygoogle.js?client='
                       f'{esc(ADSENSE)}" crossorigin="anonymous"></script>')
    doc = f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{esc(title)}</title>
<meta name="description" content="{esc(description)}">
<link rel="canonical" href="{canonical}">{robots}
<meta property="og:type" content="website"><meta property="og:site_name" content="Ticker Press">
<meta property="og:title" content="{esc(title)}"><meta property="og:description" content="{esc(description)}">
<meta property="og:url" content="{canonical}"><meta property="og:image" content="{SITE_URL}/avatar.png">
<meta name="theme-color" content="#090d16"><link rel="icon" href="/avatar.png">
<link rel="preconnect" href="https://fonts.googleapis.com"><link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Anton&family=Inter:wght@400;600;700&display=swap">
<link rel="stylesheet" href="/style.css">
</head><body>
<header><div class="wrap"><a class="logo" href="/">TICKER<b>PRESS</b></a>
<nav><a href="/">Latest buys</a><a href="/cluster-buys/">Cluster buys</a><a href="/stocks/">All stocks</a><a href="/insiders/">Top insiders</a><a href="/tools/">Tools</a><a href="/about/">About</a></nav>
<a class="btn" href="{TELEGRAM}" rel="nofollow">Free alerts</a></div></header>
<main class="wrap">{body}</main>
<footer><div class="wrap">Data from SEC Form 4 filings (EDGAR). Purchases reported with transaction code P of
{ia.money(ia.SITE_MIN_BUY)} or more; indirect and private ones are tagged. For information only, <b>not financial advice</b>: insiders buy for many reasons
and can be wrong. © {date.today().year} Ticker Press · <a href="/about/">Methodology</a> ·
<a href="/tools/">Spreadsheet tools</a> ·
<a href="{TELEGRAM}" rel="nofollow">Telegram</a> · <a href="/contact/">Contact</a> ·
<a href="/privacy/">Privacy</a> · <a href="/terms/">Terms</a></div></footer>
</body></html>
"""
    out = DIST / path / "index.html" if path else DIST / "index.html"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(doc, encoding="utf-8")


def cta(text: str = "Get insider buys on Telegram") -> str:
    return (f'<div class="cta"><p><strong>{esc(text)}</strong>Free daily recap of the biggest insider '
            f'purchases, straight from SEC filings. Real-time alerts for Premium members.</p>'
            f'<a class="btn" href="{TELEGRAM}" rel="nofollow">Join free on Telegram</a></div>')


def short(text: str, n: int) -> str:
    return text if len(text) <= n else text[:n - 1].rstrip(" ,.") + "…"


def buys_table(rows: list[dict], *, ticker_col: bool = True) -> str:
    head = ("<th>Filed</th>" + ("<th>Stock</th>" if ticker_col else "") +
            "<th>Insider</th><th class='num'>Value</th><th class='num'>Shares</th><th class='num'>Price</th>"
            "<th class='num' title='Change from the insider purchase price to the latest close, split-adjusted'>"
            "Since buy</th><th class='num'>Stake</th><th>Trade</th><th></th>")
    out = []
    for b in rows:
        stake = (("new" if ia.is_new_position(b) else "—") if b["stake_change"] is None else
                 ">+999%" if b["stake_change"] > 999 else f"+{b['stake_change']:.0f}%")
        plan = '<span class="tag" title="Pre-scheduled trade (Rule 10b5-1 plan)">10b5-1</span>' if b["plan"] else ""
        if b.get("indirect"):
            plan += f'<span class="tag" title="{esc(b.get("via") or "Held through an entity, not a personal account")}">indirect</span>'
        if b.get("offering"):
            plan += '<span class="tag" title="Private placement or offering, not an open-market buy">private</span>'
        tk = (f"<td><a class='tk' href='/stock/{slug(b['ticker'])}/'>{esc(b['ticker'])}</a>"
              f"<small>{esc(short(b['issuer'], 30))}</small></td>") if ticker_col else ""
        out.append(f"<tr><td>{fmt_date(b['filed'], False)}</td>{tk}<td>{person_link(b)}{plan}"
                   f"<small>{esc(short(b['role'], 34))}</small></td>"
                   f"<td class='num up'><b>{ia.money(b['value'])}</b></td><td class='num'>{b['shares']:,}</td>"
                   f"<td class='num'>${b['price']:,.2f}</td><td class='num'>{pct(since_buy(b))}</td>"
                   f"<td class='num'>{stake}</td><td>{fmt_date(b['trade_date'], False)}</td>"
                   f"<td><a href='{sec_url(b)}' rel='nofollow'>SEC</a></td></tr>")
    return f"<div class='tbl'><table><thead><tr>{head}</tr></thead><tbody>{''.join(out)}</tbody></table></div>"


def clusters(rows: list[dict], today: date) -> list[dict]:
    since = (today - timedelta(days=CLUSTER_DAYS)).isoformat()
    by = defaultdict(list)
    for b in rows:
        if b["filed"] >= since:
            by[b["ticker"]].append(b)
    out = []
    for ticker, bs in by.items():
        insiders = {b["owner_cik"] for b in bs}
        if len(insiders) >= 2:
            out.append({"ticker": ticker, "issuer": bs[0]["issuer"], "insiders": len(insiders),
                        "value": sum(b["value"] for b in bs), "last": max(b["filed"] for b in bs), "n": len(bs)})
    return sorted(out, key=lambda c: (-c["insiders"], -c["value"]))


def cluster_table(cs: list[dict]) -> str:
    rows = "".join(
        f"<tr><td><a class='tk' href='/stock/{slug(c['ticker'])}/'>{esc(c['ticker'])}</a></td><td>{esc(short(c['issuer'], 40))}</td>"
        f"<td class='num'><b>{c['insiders']}</b></td><td class='num'>{c['n']}</td>"
        f"<td class='num up'><b>{ia.money(c['value'])}</b></td><td>{fmt_date(c['last'], False)}</td></tr>" for c in cs)
    return ("<div class='tbl'><table><thead><tr><th>Ticker</th><th>Company</th><th class='num'>Insiders</th>"
            f"<th class='num'>Buys</th><th class='num'>Total</th><th>Last filed</th></tr></thead><tbody>{rows}</tbody></table></div>")


def stock_page(ticker: str, bs: list[dict], today: date) -> tuple[str, bool]:
    bs = sorted(bs, key=lambda b: (b["filed"], b["acc"]), reverse=True)
    issuer, last = bs[0]["issuer"], bs[0]
    year_ago = (today - timedelta(days=365)).isoformat()
    recent = [b for b in bs if b["filed"] >= year_ago]
    total, insiders = sum(b["value"] for b in recent), {b["owner_cik"] for b in recent}
    top = max(bs, key=lambda b: b["value"])
    indexable = len(bs) >= INDEX_MIN_BUYS or top["value"] >= INDEX_MIN_VALUE

    if recent:
        summary = (f"In the last 12 months, {len(insiders)} insider{'s' if len(insiders) != 1 else ''} of {esc(issuer)} "
                   f"bought <b>{ia.money(total)}</b> of {esc(ticker)} stock on the open market in {len(recent)} "
                   f"reported purchase{'s' if len(recent) != 1 else ''}.")
    else:
        summary = f"No open-market insider purchases of {esc(ticker)} were reported in the last 12 months."
    summary += (f" The most recent was filed on {fmt_date(last['filed'])}: {esc(person(last))} ({esc(last['role'])}) "
                f"bought {last['shares']:,} shares at ${last['price']:,.2f}, worth {ia.money(last['value'])}.")
    if top is not last:
        summary += (f" The largest purchase on record here is {ia.money(top['value'])} by {esc(person(top))} "
                    f"on {fmt_date(top['trade_date'] or top['filed'])}.")
    plans = sum(b["plan"] for b in bs)
    if plans:
        summary += f" {plans} of these trades were pre-scheduled under a Rule 10b5-1 plan."
    ret, last_ret = total_return(bs), since_buy(last)
    if ret is not None:
        p = PRICE[ticker]
        summary += (f" {esc(ticker)} last closed at ${p['close']:,.2f} on {fmt_date(p['date'])}: taken together, "
                    f"the insider purchases listed here are {'up' if ret >= 0 else 'down'} "
                    f"<b>{abs(ret) * 100:.0f}%</b> since they were made (split-adjusted, weighted by amount).")

    body = (f"<h1>{esc(ticker)} insider buying</h1>"
            f"<p class='lead'>Every open-market purchase of {esc(issuer)} ({esc(ticker)}) stock by its officers, "
            f"directors and major shareholders, from SEC Form 4 filings.</p>"
            f"<div class='stats'><div class='stat'><span>Bought, last 12 months</span><b>{ia.money(total)}</b></div>"
            f"<div class='stat'><span>Insiders buying (12m)</span><b>{len(insiders)}</b></div>"
            f"<div class='stat'><span>Purchases on record</span><b>{len(bs)}</b></div>"
            f"<div class='stat'><span>Last purchase filed</span><b>{fmt_date(last['filed'])}</b></div>"
            + (f"<div class='stat'><span>Since last insider buy</span><b>{pct(last_ret)}</b></div>"
               if last_ret is not None else "") + "</div>"
            f"<div class='prose'><p>{summary}</p></div>"
            f"{cta(f'Get the next {ticker} insider buy on Telegram')}"
            f"<h2>All {esc(ticker)} insider purchases</h2>{buys_table(bs, ticker_col=False)}")
    title = f"{ticker} Insider Buying: {issuer} Insider Purchases (SEC Form 4) | Ticker Press"
    desc = (f"{issuer} ({ticker}) insiders bought {ia.money(total)} of stock in the last 12 months. "
            f"See every insider purchase: who bought, how many shares, at what price.") if recent else \
           f"Every open-market insider purchase of {issuer} ({ticker}) stock, from SEC Form 4 filings."
    page(f"stock/{slug(ticker)}/", title, desc, body, noindex=not indexable, ads=True)
    return issuer, indexable


def insider_urls(by_owner: dict[str, list[dict]]) -> dict[str, str]:
    """owner_cik -> /insider/<nombre>/ para los insiders con pagina. Si dos comparten nombre, se anade el CIK."""
    taken, out = set(), {}
    for cik in sorted(c for c, bs in by_owner.items() if len(bs) >= INSIDER_MIN_BUYS):
        s = re.sub(r"[^a-z0-9]+", "-", ia.smart_title(by_owner[cik][0]["owner"]).lower()).strip("-") or "insider"
        if s in taken:
            s = f"{s}-{int(cik)}"
        taken.add(s)
        out[cik] = f"/insider/{s}/"
    return out


def insider_indexable(bs: list[dict]) -> bool:
    return len(bs) >= INSIDER_INDEX_BUYS or sum(b["value"] for b in bs) >= INSIDER_INDEX_VALUE


def insider_page(cik: str, bs: list[dict], names: dict[str, str]) -> bool:
    bs = sorted(bs, key=lambda b: (b["filed"], b["acc"]), reverse=True)
    name, last = ia.smart_title(bs[0]["owner"]), bs[0]
    total, top = sum(b["value"] for b in bs), max(bs, key=lambda b: b["value"])
    by_stock = defaultdict(list)
    for b in bs:
        by_stock[b["ticker"]].append(b)
    indexable = insider_indexable(bs)

    summary = (f"{esc(name)} has made <b>{len(bs)}</b> open-market purchases worth <b>{ia.money(total)}</b> in "
               f"{len(by_stock)} compan{'ies' if len(by_stock) != 1 else 'y'} since {fmt_date(bs[-1]['filed'])}, "
               f"according to SEC Form 4 filings. The most recent was filed on {fmt_date(last['filed'])}: "
               f"{last['shares']:,} shares of {esc(last['ticker'])} at ${last['price']:,.2f} "
               f"({esc(last['role'])}), worth {ia.money(last['value'])}.")
    if top is not last:
        summary += (f" The largest was {ia.money(top['value'])} of {esc(top['ticker'])} "
                    f"on {fmt_date(top['trade_date'] or top['filed'])}.")
    ret = total_return(bs)
    if ret is not None:
        summary += (f" Taken together, these purchases are {'up' if ret >= 0 else 'down'} <b>{abs(ret) * 100:.0f}%</b> "
                    f"at the latest closing prices (split-adjusted, weighted by amount).")
    stocks = sorted(by_stock.items(), key=lambda kv: -sum(b["value"] for b in kv[1]))
    rows = "".join(
        f"<tr><td><a class='tk' href='/stock/{slug(t)}/'>{esc(t)}</a></td><td>{esc(short(names.get(t, sb[0]['issuer']), 40))}</td>"
        f"<td>{esc(short(sb[0]['role'], 34))}</td><td class='num'>{len(sb)}</td>"
        f"<td class='num up'><b>{ia.money(sum(b['value'] for b in sb))}</b></td><td class='num'>{pct(total_return(sb))}</td>"
        f"<td>{fmt_date(max(b['filed'] for b in sb), False)}</td></tr>" for t, sb in stocks)
    body = (f"<h1>{esc(name)}: insider buys</h1>"
            f"<p class='lead'>Every open-market stock purchase reported by {esc(name)} to the SEC on Form 4.</p>"
            f"<div class='stats'><div class='stat'><span>Total bought</span><b>{ia.money(total)}</b></div>"
            f"<div class='stat'><span>Purchases</span><b>{len(bs)}</b></div>"
            f"<div class='stat'><span>Companies</span><b>{len(by_stock)}</b></div>"
            f"<div class='stat'><span>Last purchase filed</span><b>{fmt_date(last['filed'])}</b></div>"
            + (f"<div class='stat'><span>Return on these buys</span><b>{pct(ret)}</b></div>" if ret is not None else "")
            + "</div>"
            f"<div class='prose'><p>{summary}</p></div>"
            f"<h2>Companies {esc(short(name, 40))} bought</h2><div class='tbl'><table><thead><tr><th>Ticker</th>"
            f"<th>Company</th><th>Role</th><th class='num'>Buys</th><th class='num'>Total</th>"
            f"<th class='num'>Since buy</th><th>Last filed</th>"
            f"</tr></thead><tbody>{rows}</tbody></table></div>"
            f"{cta(f'Get the next {short(name, 40)} purchase on Telegram')}"
            f"<h2>All purchases</h2>{buys_table(bs)}")
    title = f"{short(name, 45)} Stock Purchases: Insider Buys (SEC Form 4) | Ticker Press"
    desc = (f"{name} bought {ia.money(total)} of stock in {len(bs)} open-market purchases "
            f"({', '.join(t for t, _ in stocks[:3])}). Every trade from SEC Form 4 filings.")
    page(INSIDER_URLS[cik].strip("/") + "/", title, desc, body, noindex=not indexable, ads=True)
    return indexable


def insider_table(rows: list[tuple[str, list[dict]]]) -> str:
    out = "".join(
        f"<tr><td><a href='{INSIDER_URLS[cik]}'><b>{esc(short(ia.smart_title(bs[0]['owner']), 40))}</b></a></td>"
        f"<td>{', '.join(sorted({esc(b['ticker']) for b in bs})[:4])}</td><td class='num'>{len(bs)}</td>"
        f"<td class='num up'><b>{ia.money(sum(b['value'] for b in bs))}</b></td><td class='num'>{pct(total_return(bs))}</td>"
        f"<td>{fmt_date(max(b['filed'] for b in bs), False)}</td></tr>" for cik, bs in rows)
    return ("<div class='tbl'><table><thead><tr><th>Insider</th><th>Stocks</th><th class='num'>Buys</th>"
            f"<th class='num'>Total</th><th class='num'>Since buy</th><th>Last filed</th></tr></thead>"
            f"<tbody>{out}</tbody></table></div>")


def legal_pages(sitemap: list, today) -> None:
    """Contacto, privacidad y terminos: AdSense (y cualquier visitante) espera encontrarlos."""
    updated = "November 5, 2026"
    mail = f'<a href="mailto:{CONTACT}">{CONTACT}</a>'
    ads_text = ("""<h2>Advertising and cookies</h2>
<p>We use Google AdSense to show ads on some pages. Google and its partners use cookies to serve ads based on your
visits to this and other websites. Google's use of advertising cookies lets it and its partners serve ads based on
those visits. You can opt out of personalised advertising in
<a href="https://adssettings.google.com/" rel="nofollow">Google's Ads Settings</a>, and learn more at
<a href="https://policies.google.com/technologies/partner-sites" rel="nofollow">How Google uses information from sites
that use its services</a> and <a href="https://www.aboutads.info/choices/" rel="nofollow">aboutads.info</a>. Visitors
from the European Economic Area, the UK and Switzerland are asked for consent before any personalised ads or
advertising cookies are used.</p>""" if ADSENSE else
                """<h2>Advertising and cookies</h2>
<p>This site does not currently show ads or set advertising cookies. If that changes, this policy will be updated
first, and visitors from the European Economic Area, the UK and Switzerland will be asked for consent.</p>""")

    page("contact/", "Contact | Ticker Press", "How to contact Ticker Press.",
         f"""<h1>Contact</h1><div class="prose">
<p>Found a wrong number, a missing filing or a broken page? Have a question about the data, a partnership or
advertising? Write to us at {mail}. We usually answer within a few days.</p>
<p>For corrections, please include the stock ticker or the link to the page and, if you can, the SEC filing.</p>
<p>Insider alerts: <a href="{TELEGRAM}" rel="nofollow">Telegram</a> · How the data works:
<a href="/about/">About &amp; methodology</a></p></div>""")

    page("privacy/", "Privacy Policy | Ticker Press", "How Ticker Press handles data and cookies.",
         f"""<h1>Privacy Policy</h1><div class="prose">
<p><i>Last updated: {updated}</i></p>
<p>Ticker Press (tickerpress.net) is a free website about insider buying in US stocks. This policy explains what
information is collected when you visit it.</p>
<h2>What we collect</h2>
<p>We have no user accounts, forms or newsletters on this site, and we do not ask for your name or email. If you write
to us by email, we use your message only to answer it.</p>
<h2>Analytics and hosting</h2>
<p>The site is hosted on Cloudflare, which processes technical data such as your IP address to deliver pages and
protect the site. We use Cloudflare Web Analytics to count visits; it does not use cookies and does not track you
across websites.</p>
{ads_text}
<h2>Third-party links</h2>
<p>Pages link to the SEC, Telegram, Etsy and other sites. Their own privacy policies apply when you visit them.</p>
<h2>Your rights</h2>
<p>Depending on where you live (for example under the GDPR or the CCPA) you may ask about, correct or delete data
related to you. Contact us at {mail}.</p>
<h2>Changes</h2>
<p>We may update this policy; the date above shows the latest version.</p></div>""")

    page("terms/", "Terms of Use & Disclaimer | Ticker Press", "Terms of use and financial disclaimer of Ticker Press.",
         f"""<h1>Terms of Use &amp; Disclaimer</h1><div class="prose">
<p><i>Last updated: {updated}</i></p>
<h2>Not financial advice</h2>
<p>Everything on Ticker Press is for information and education only. Nothing here is investment, tax or legal advice
or a recommendation to buy or sell any security. Insiders buy shares for many reasons and can be wrong; past
performance does not predict future results. Do your own research and consider a licensed adviser before investing.</p>
<h2>Accuracy of the data</h2>
<p>Data comes from public SEC Form 4 filings and public market prices, processed automatically. Filings can be amended
and prices can be delayed or wrong, so we cannot guarantee that every figure is complete, current or correct. Always
check the original SEC filing linked on each page.</p>
<h2>Use of the site</h2>
<p>You may read and share our pages and link to them. Please do not copy the site in bulk or scrape it in a way that
harms its availability. The site is provided "as is", without warranties, and we are not liable for losses arising
from its use.</p>
<h2>Contact</h2><p>{mail}</p></div>""")
    sitemap += [("contact/", today.isoformat()), ("privacy/", today.isoformat()), ("terms/", today.isoformat())]


def build() -> None:
    rows = sorted(load_buys().values(), key=lambda b: (b["filed"], b["value"]), reverse=True)
    for b in rows:
        b["issuer"] = clean_issuer(b["issuer"])
        b["ticker"] = clean_ticker(b["ticker"])
    rows = [b for b in rows if b["ticker"]]
    if not rows:
        raise SystemExit("No hay datos: ejecuta antes 'backfill'")
    if DIST.exists():
        shutil.rmtree(DIST)
    DIST.mkdir()
    (DIST / "style.css").write_text(CSS.strip(), encoding="utf-8")
    shutil.copy("branding/avatar.png", DIST / "avatar.png")
    today = date.fromisoformat(rows[0]["filed"])
    sitemap = [("", today.isoformat())]
    load_prices(today)

    # Insiders: las URLs se calculan antes para enlazar sus nombres desde todas las tablas
    by_owner = defaultdict(list)
    for b in rows:
        by_owner[b["owner_cik"]].append(b)
    INSIDER_URLS.clear()
    INSIDER_URLS.update(insider_urls(by_owner))

    # Acciones
    by_ticker = defaultdict(list)
    for b in rows:
        by_ticker[b["ticker"]].append(b)
    names = {}
    for ticker, bs in by_ticker.items():
        issuer, indexable = stock_page(ticker, bs, today)
        names[ticker] = issuer
        if indexable:
            sitemap.append((f"stock/{slug(ticker)}/", max(b["filed"] for b in bs)))

    for cik in INSIDER_URLS:
        if insider_page(cik, by_owner[cik], names):
            sitemap.append((INSIDER_URLS[cik].strip("/") + "/", max(b["filed"] for b in by_owner[cik])))
    year_ago = (today - timedelta(days=365)).isoformat()
    top_insiders = sorted(((c, [b for b in by_owner[c] if b["filed"] >= year_ago]) for c in INSIDER_URLS
                           if insider_indexable(by_owner[c])), key=lambda x: -sum(b["value"] for b in x[1]))
    top_insiders = [x for x in top_insiders if x[1]]
    page("insiders/", "Top Insider Buyers: Executives & Funds Buying Stock | Ticker Press",
         "The insiders and funds that bought the most stock on the open market in the last 12 months, "
         "from SEC Form 4 filings. Updated daily.",
         f"<h1>Top insider buyers</h1><p class='lead'>The executives, directors and investment funds that put the most "
         f"money into their own companies' stock on the open market in the last 12 months.</p>"
         f"{insider_table(top_insiders[:200])}{cta()}")
    sitemap.append(("insiders/", today.isoformat()))

    # Dias
    by_day = defaultdict(list)
    for b in rows:
        by_day[b["filed"]].append(b)
    days = sorted(by_day, reverse=True)
    for i, d in enumerate(days):
        bs = sorted(by_day[d], key=lambda b: -b["value"])
        nav = " · ".join(x for x in (
            f"<a href='/day/{days[i + 1]}/'>← {fmt_date(days[i + 1])}</a>" if i + 1 < len(days) else "",
            f"<a href='/day/{days[i - 1]}/'>{fmt_date(days[i - 1])} →</a>" if i > 0 else "") if x)
        body = (f"<h1>Insider buying on {fmt_date(d)}</h1>"
                f"<p class='lead'>{len(bs)} open-market insider purchases filed with the SEC on {fmt_date(d)}, "
                f"worth {ia.money(sum(b['value'] for b in bs))} in total. Largest: {esc(bs[0]['ticker'])} "
                f"({ia.money(bs[0]['value'])}).</p><p>{nav}</p>{buys_table(bs)}{cta()}")
        page(f"day/{d}/", f"Insider Buying {fmt_date(d)}: {len(bs)} Insider Purchases | Ticker Press",
             f"All {len(bs)} insider stock purchases filed with the SEC on {fmt_date(d)}. "
             f"Biggest: {bs[0]['ticker']} {ia.money(bs[0]['value'])}.", body, ads=True)
        sitemap.append((f"day/{d}/", d))

    # Cluster buys
    cs = clusters(rows, today)
    page("cluster-buys/", "Cluster Buys: Stocks Several Insiders Are Buying | Ticker Press",
         f"Stocks where two or more insiders bought shares on the open market in the last {CLUSTER_DAYS} days, "
         "from SEC Form 4 filings. Updated daily.",
         f"<h1>Insider cluster buys</h1><p class='lead'>Stocks where <b>two or more different insiders</b> bought "
         f"shares on the open market in the last {CLUSTER_DAYS} days. When several people with inside knowledge of "
         f"a company buy at the same time, it is one of the most watched signals in insider data.</p>"
         f"{cluster_table(cs)}{cta('Get cluster buys the moment they are filed')}")
    sitemap.append(("cluster-buys/", today.isoformat()))

    # Directorio A-Z
    letters = defaultdict(list)
    for t in sorted(names):
        letters[t[0] if t[0].isalpha() else "0"].append(t)
    letter_nav = "<div class='letters'>" + "".join(
        f"<a href='/stocks/{l.lower()}/'>{'0-9' if l == '0' else l}</a>" for l in sorted(letters)) + "</div>"
    page("stocks/", "Insider Buying by Stock: A-Z Directory | Ticker Press",
         f"Insider purchases for {len(names):,} US stocks, from SEC Form 4 filings. Pick a ticker.",
         f"<h1>Insider buying by stock</h1><p class='lead'>{len(names):,} stocks with open-market insider "
         f"purchases on record. Pick a letter.</p>{letter_nav}")
    sitemap.append(("stocks/", today.isoformat()))
    for l, tickers in letters.items():
        links = "".join(f"<a href='/stock/{slug(t)}/'><b>{esc(t)}</b> · {esc(names[t][:28])}</a>" for t in tickers)
        label = "0-9" if l == "0" else l
        page(f"stocks/{l.lower()}/", f"Insider Buying: Stocks Starting With {label} | Ticker Press",
             f"Insider purchases for {len(tickers)} stocks whose ticker starts with {label}.",
             f"<h1>Stocks: {label}</h1>{letter_nav}<div class='grid'>{links}</div>")
        sitemap.append((f"stocks/{l.lower()}/", today.isoformat()))

    # Portada
    week = (today - timedelta(days=LATEST_DAYS)).isoformat()
    latest = [b for b in rows if b["filed"] >= week]
    month = [b for b in rows if b["filed"] >= (today - timedelta(days=30)).isoformat()]
    biggest = sorted(month, key=lambda b: -b["value"])[:15]
    # Mejores compras de 6 meses: de 250.000 $ o mas y a 1 $ o mas, para que no las dominen apuestas diminutas
    # ni acciones de centimos
    half_year = (today - timedelta(days=182)).isoformat()
    best = sorted((b for b in rows if b["filed"] >= half_year and b["value"] >= 250_000 and b["price"] >= 1
                   and since_buy(b) is not None),
                  key=lambda b: -since_buy(b))[:10]
    body = (f"<h1>What are insiders buying?</h1>"
            f"<p class='lead'>When CEOs, CFOs and directors buy their own company's stock on the open market, "
            f"they bet their own money. Every one of those trades is reported to the SEC on Form 4. "
            f"Here is every insider purchase, updated daily.</p>"
            f"<div class='stats'><div class='stat'><span>Insider buys, last 7 days</span><b>{len(latest)}</b></div>"
            f"<div class='stat'><span>Value, last 7 days</span><b>{ia.money(sum(b['value'] for b in latest))}</b></div>"
            f"<div class='stat'><span>Cluster buys (30d)</span><b>{len(cs)}</b></div>"
            f"<div class='stat'><span>Stocks tracked</span><b>{len(names):,}</b></div></div>"
            f"{cta()}"
            f"<h2>Latest insider buys</h2>{buys_table(latest[:100])}"
            f"<p><a href='/day/{today.isoformat()}/'>All buys filed {fmt_date(today.isoformat())} →</a></p>"
            f"<h2>Top cluster buys (last {CLUSTER_DAYS} days)</h2>{cluster_table(cs[:10])}"
            f"<p><a href='/cluster-buys/'>All cluster buys →</a></p>"
            f"<h2>Biggest insider buys this month</h2>{buys_table(biggest)}"
            + (f"<h2>Best-performing insider buys (last 6 months)</h2><p>Purchases of $250K or more at $1 or more per "
               f"share, ranked by how "
               f"much the stock has moved since the insider bought (latest close, split-adjusted).</p>{buys_table(best)}"
               if best else "") +
            f"<h2>Top insider buyers (last 12 months)</h2>{insider_table(top_insiders[:10])}"
            f"<p><a href='/insiders/'>All top insiders →</a></p>")
    page("", "Insider Buying Today: Latest SEC Form 4 Insider Purchases | Ticker Press",
         "Track what company insiders are buying: every open-market insider purchase from SEC Form 4 filings, "
         "cluster buys and the biggest buys, updated daily. Free alerts on Telegram.", body)

    page("about/", "About & Methodology | Ticker Press",
         "How Ticker Press tracks insider buying from SEC Form 4 filings.",
         f"""<h1>About Ticker Press</h1><div class="prose">
<p>Ticker Press tracks insider buying in US stocks. Every page is built from <b>SEC Form 4 filings</b>, the forms
company insiders must file within two business days of trading their company's shares.</p>
<h2>What we count</h2>
<p>Purchases of common stock reported with transaction code “P” (“open market or private purchase”) of
{ia.money(ia.SITE_MIN_BUY)} or more: the insider paid cash. Option exercises, grants, gifts and sales are not included.
Several purchases in one filing are added up and shown at their average price. “Stake” is how much the purchase
increased the insider's holdings.</p>
<p>Not every code-P purchase is the same signal, so we tag them:</p>
<ul>
<li><span class="tag">indirect</span> the shares were bought through an entity (a fund, trust, company or family
member), not a personal account. Hover the tag to see which one.</li>
<li><span class="tag">private</span> the filing's footnotes describe a private placement or an offering: the shares
came from the company, not from the open market.</li>
<li><span class="tag">10b5-1</span> the trade was pre-scheduled under a trading plan, usually a weaker signal.</li>
</ul>
<h2>Performance since the purchase</h2>
<p>“Since buy” compares the price the insider paid with the stock's latest closing price, adjusted for any stock splits
after the trade. For several purchases it is weighted by the amount invested in each one. Dividends are not included.
Prices come from public market data and can be delayed or occasionally wrong; past performance says nothing about the
future.</p>
<h2>Cluster buys</h2>
<p>A stock is a cluster buy when at least two different insiders bought on the open market within {CLUSTER_DAYS} days.</p>
<h2>Sources and updates</h2>
<p>Historical data comes from the SEC's quarterly Insider Transactions Data Sets; new filings are read from EDGAR every
hour on US business days and this site is rebuilt daily. Figures are shown as filed; always check the linked SEC filing.</p>
<h2>Not financial advice</h2>
<p>This site is for information and education only. Insiders buy for many reasons and can be wrong. Do your own
research before investing.</p>
<p>Questions: <a href="mailto:tickerpressyt@gmail.com">tickerpressyt@gmail.com</a> ·
Alerts: <a href="{TELEGRAM}" rel="nofollow">Telegram</a></p></div>""")
    sitemap.append(("about/", today.isoformat()))

    cards = "".join(f'<div class="stat"><b>{esc(name)}</b><p style="color:var(--muted);margin:6px 0 12px">{esc(text)}</p>'
                    f'<a class="btn" href="https://www.etsy.com/listing/{lid}" rel="nofollow">See it on Etsy</a></div>'
                    for name, lid, text in TOOLS)
    page("tools/", "Spreadsheet Tools for Investors | Ticker Press",
         "Excel and Google Sheets templates to track your net worth, budget, debt payoff and savings.",
         f"<h1>Spreadsheet tools</h1><p class='lead'>Simple Excel and Google Sheets templates to track your own money: "
         f"net worth, budget, debt and savings. Type your numbers in and everything is calculated for you.</p>"
         f"<div class='stats'>{cards}</div>"
         f"<p><a href='{ETSY_SHOP}' rel='nofollow'>See all 15 templates in our Etsy shop →</a></p>{cta()}")
    sitemap.append(("tools/", today.isoformat()))

    legal_pages(sitemap, today)

    page("404/", "Page not found | Ticker Press", "Page not found.",
         "<h1>Page not found</h1><p class='lead'>Try the <a href='/stocks/'>stock directory</a> or the "
         "<a href='/'>latest insider buys</a>.</p>", noindex=True)
    shutil.move(DIST / "404" / "index.html", DIST / "404.html")
    (DIST / "404").rmdir()

    urls = "".join(f"<url><loc>{SITE_URL}/{p}</loc><lastmod>{d}</lastmod></url>" for p, d in sitemap)
    (DIST / "sitemap.xml").write_text('<?xml version="1.0" encoding="UTF-8"?>\n<urlset xmlns='
                                      f'"http://www.sitemaps.org/schemas/sitemap/0.9">{urls}</urlset>', encoding="utf-8")
    (DIST / "robots.txt").write_text(f"User-agent: *\nAllow: /\nSitemap: {SITE_URL}/sitemap.xml\n", encoding="utf-8")
    if ADSENSE:   # vendedor autorizado de los anuncios (AdSense lo pide en la raiz del dominio)
        pub = ADSENSE.removeprefix("ca-")
        (DIST / "ads.txt").write_text(f"google.com, {pub}, DIRECT, f08c47fec0942fa0\n", encoding="utf-8")
    n_pages = sum(1 for _ in DIST.rglob("*.html"))
    print(f"   . web generada en {DIST}/: {n_pages} paginas, {len(sitemap)} en el sitemap, "
          f"{len(names)} acciones ({sum(1 for p, _ in sitemap if p.startswith('stock/'))} indexables)")


if __name__ == "__main__":
    cmd = sys.argv[1:2]
    if cmd == ["backfill"]:
        backfill(int(sys.argv[2]) if len(sys.argv) > 2 else 4)
    elif cmd == ["update"]:
        update()
    elif cmd == ["prices"]:
        update_prices()
    elif cmd == ["enrich"]:
        enrich()
    elif cmd == ["build"]:
        build()
    else:
        print("Uso: python insider_site.py backfill [trimestres] | update | prices | build")
