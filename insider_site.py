"""
================================================================================
 insider_site.py - Web tickerpress.net: compras de directivos por accion
================================================================================

 Web estatica (Cloudflare Pages) con los datos del bot Insider Alerts:
   /                      ultimas compras, cluster buys y mayores compras
   /stock/<ticker>/       todas las compras de directivos de una accion
   /day/<fecha>/          compras presentadas ese dia
   /cluster-buys/         acciones con varios directivos comprando (30 dias)
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
DATA_FILE  = Path("site_data/insider_buys.json")
CHECKS     = Path("site_data/price_checks.json")   # resultado de ia.plausible() por Form 4
DIST       = Path("site_dist")
CACHE      = Path("data/sec")
DATASETS   = "https://www.sec.gov/data-research/sec-markets-data/insider-transactions-data-sets"
DAILY_IDX  = "https://www.sec.gov/Archives/edgar/daily-index/{y}/QTR{q}/form.{d}.idx"

FIELDS = ("acc", "ticker", "issuer", "issuer_cik", "owner", "owner_cik", "joint", "role", "shares",
          "value", "price", "after", "stake_change", "trade_date", "filed", "plan")
INDEX_MIN_BUYS  = 2          # una accion se indexa con 2+ compras...
INDEX_MIN_VALUE = 100_000    # ...o con una compra de 100.000 $ o mas
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
    return {b["acc"]: b for b in rows if ia.plausible(b)}


def save_buys(buys: dict[str, dict]) -> None:
    rows = sorted(buys.values(), key=lambda b: (b["filed"], b["acc"]), reverse=True)
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
        t = totals.setdefault(r["ACCESSION_NUMBER"], {"shares": 0.0, "value": 0.0, "after": 0.0, "dates": []})
        t["shares"] += shares
        t["value"] += shares * price
        t["after"] = after or t["after"]
        t["dates"].append(_sec_date(r["TRANS_DATE"]))
    totals = {acc: t for acc, t in totals.items() if t["value"] >= ia.SITE_MIN_BUY}  # plausible(): en load_buys

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


def update() -> None:
    buys = load_buys()
    events = ia.load_state().get("events", [])
    new = [record({**e, "issuer": clean_issuer(e["issuer"])})
           for e in events if e["kind"] == "buy" and e["acc"] not in buys and ia.plausible(e)]
    buys.update({b["acc"]: b for b in new})
    print(f"   . {len(new)} compras nuevas del bot")
    save_buys(buys)


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
    return ia.smart_title(b["owner"]) + (f" +{b['joint']}" if b["joint"] else "")


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
.up{color:var(--green)}.tag{font-size:12px;color:var(--muted);border:1px solid var(--line);border-radius:6px;padding:1px 6px;margin-left:6px}
.cta{display:flex;gap:16px;align-items:center;justify-content:space-between;flex-wrap:wrap;background:var(--green-soft);
border:1px solid rgba(0,214,125,.35);border-radius:14px;padding:18px 20px;margin:28px 0}
.cta p{margin:0;max-width:640px}.cta strong{display:block;font-size:18px}
.letters{display:flex;flex-wrap:wrap;gap:8px;margin:16px 0}.letters a{border:1px solid var(--line);border-radius:8px;padding:6px 11px;background:var(--card)}
.grid{columns:4 180px;column-gap:24px}.grid a{display:block;padding:3px 0;break-inside:avoid}
.prose{max-width:760px}.prose p{color:#d5dbe5}
footer{border-top:1px solid var(--line);margin-top:60px;padding:28px 0;color:var(--muted);font-size:13.5px}
@media(max-width:640px){header nav{display:none}h2{font-size:19px}}
"""


def page(path: str, title: str, description: str, body: str, *, noindex: bool = False) -> None:
    canonical = f"{SITE_URL}/{path}" if path else f"{SITE_URL}/"
    robots = '<meta name="robots" content="noindex,follow">' if noindex else ""
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
<nav><a href="/">Latest buys</a><a href="/cluster-buys/">Cluster buys</a><a href="/stocks/">All stocks</a><a href="/about/">About</a></nav>
<a class="btn" href="{TELEGRAM}" rel="nofollow">Free alerts</a></div></header>
<main class="wrap">{body}</main>
<footer><div class="wrap">Data from SEC Form 4 filings (EDGAR). Open-market purchases (transaction code P) of
{ia.money(ia.SITE_MIN_BUY)} or more. For information only, <b>not financial advice</b>: insiders buy for many reasons
and can be wrong. © {date.today().year} Ticker Press · <a href="/about/">Methodology</a> ·
<a href="{TELEGRAM}" rel="nofollow">Telegram</a></div></footer>
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
            "<th class='num'>Stake</th><th>Trade</th><th></th>")
    out = []
    for b in rows:
        stake = (("new" if ia.is_new_position(b) else "—") if b["stake_change"] is None else
                 ">+999%" if b["stake_change"] > 999 else f"+{b['stake_change']:.0f}%")
        plan = '<span class="tag" title="Pre-scheduled trade (Rule 10b5-1 plan)">10b5-1</span>' if b["plan"] else ""
        tk = (f"<td><a class='tk' href='/stock/{slug(b['ticker'])}/'>{esc(b['ticker'])}</a>"
              f"<small>{esc(short(b['issuer'], 30))}</small></td>") if ticker_col else ""
        out.append(f"<tr><td>{fmt_date(b['filed'], False)}</td>{tk}<td>{esc(short(person(b), 30))}{plan}"
                   f"<small>{esc(short(b['role'], 34))}</small></td>"
                   f"<td class='num up'><b>{ia.money(b['value'])}</b></td><td class='num'>{b['shares']:,}</td>"
                   f"<td class='num'>${b['price']:,.2f}</td>"
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

    body = (f"<h1>{esc(ticker)} insider buying</h1>"
            f"<p class='lead'>Every open-market purchase of {esc(issuer)} ({esc(ticker)}) stock by its officers, "
            f"directors and major shareholders, from SEC Form 4 filings.</p>"
            f"<div class='stats'><div class='stat'><span>Bought, last 12 months</span><b>{ia.money(total)}</b></div>"
            f"<div class='stat'><span>Insiders buying (12m)</span><b>{len(insiders)}</b></div>"
            f"<div class='stat'><span>Purchases on record</span><b>{len(bs)}</b></div>"
            f"<div class='stat'><span>Last purchase filed</span><b>{fmt_date(last['filed'])}</b></div></div>"
            f"<div class='prose'><p>{summary}</p></div>"
            f"{cta(f'Get the next {ticker} insider buy on Telegram')}"
            f"<h2>All {esc(ticker)} insider purchases</h2>{buys_table(bs, ticker_col=False)}")
    title = f"{ticker} Insider Buying: {issuer} Insider Purchases (SEC Form 4) | Ticker Press"
    desc = (f"{issuer} ({ticker}) insiders bought {ia.money(total)} of stock in the last 12 months. "
            f"See every insider purchase: who bought, how many shares, at what price.") if recent else \
           f"Every open-market insider purchase of {issuer} ({ticker}) stock, from SEC Form 4 filings."
    page(f"stock/{slug(ticker)}/", title, desc, body, noindex=not indexable)
    return issuer, indexable


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
             f"Biggest: {bs[0]['ticker']} {ia.money(bs[0]['value'])}.", body)
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
            f"<h2>Biggest insider buys this month</h2>{buys_table(biggest)}")
    page("", "Insider Buying Today: Latest SEC Form 4 Insider Purchases | Ticker Press",
         "Track what company insiders are buying: every open-market insider purchase from SEC Form 4 filings, "
         "cluster buys and the biggest buys, updated daily. Free alerts on Telegram.", body)

    page("about/", "About & Methodology | Ticker Press",
         "How Ticker Press tracks insider buying from SEC Form 4 filings.",
         f"""<h1>About Ticker Press</h1><div class="prose">
<p>Ticker Press tracks insider buying in US stocks. Every page is built from <b>SEC Form 4 filings</b>, the forms
company insiders must file within two business days of trading their company's shares.</p>
<h2>What we count</h2>
<p>Only <b>open-market purchases</b> (transaction code “P”) of {ia.money(ia.SITE_MIN_BUY)} or more of common stock:
the insider paid cash at market prices. Option exercises, grants, gifts and sales are not included. Several purchases
in one filing are added up and shown at their average price. “Stake” is how much the purchase increased the
insider's holdings. Trades marked <span class="tag">10b5-1</span> were pre-scheduled under a trading plan and are
usually a weaker signal.</p>
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

    page("404/", "Page not found | Ticker Press", "Page not found.",
         "<h1>Page not found</h1><p class='lead'>Try the <a href='/stocks/'>stock directory</a> or the "
         "<a href='/'>latest insider buys</a>.</p>", noindex=True)
    shutil.move(DIST / "404" / "index.html", DIST / "404.html")
    (DIST / "404").rmdir()

    urls = "".join(f"<url><loc>{SITE_URL}/{p}</loc><lastmod>{d}</lastmod></url>" for p, d in sitemap)
    (DIST / "sitemap.xml").write_text('<?xml version="1.0" encoding="UTF-8"?>\n<urlset xmlns='
                                      f'"http://www.sitemaps.org/schemas/sitemap/0.9">{urls}</urlset>', encoding="utf-8")
    (DIST / "robots.txt").write_text(f"User-agent: *\nAllow: /\nSitemap: {SITE_URL}/sitemap.xml\n", encoding="utf-8")
    n_pages = sum(1 for _ in DIST.rglob("*.html"))
    print(f"   . web generada en {DIST}/: {n_pages} paginas, {len(sitemap)} en el sitemap, "
          f"{len(names)} acciones ({sum(1 for p, _ in sitemap if p.startswith('stock/'))} indexables)")


if __name__ == "__main__":
    cmd = sys.argv[1:2]
    if cmd == ["backfill"]:
        backfill(int(sys.argv[2]) if len(sys.argv) > 2 else 4)
    elif cmd == ["update"]:
        update()
    elif cmd == ["build"]:
        build()
    else:
        print("Uso: python insider_site.py backfill [trimestres] | update | build")
