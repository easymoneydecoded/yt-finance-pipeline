"""
================================================================================
 morning_pipeline.py — Etapa 1 del canal de YouTube automatico
================================================================================

 Que hace:
   1. Lee noticias financieras de RSS (Reuters, Yahoo Finance, MarketWatch)
   2. Saca un snapshot de mercado con yfinance (indices + cripto)
   3. Saca indicadores economicos de la FRED (tipos, inflacion, paro)
   4. Saca los hilos calientes de Reddit (personalfinance, investing, FIRE)
   5. Manda todo a Groq y obtiene 5 ideas de video en ingles con hook listo

 Salida:
   data/ideas_YYYY-MM-DD.json  -> estructurado, para la siguiente etapa
   data/ideas_YYYY-MM-DD.md    -> resumen legible para ti

 Coste: 0 EUR (todos los servicios son free tier)

================================================================================
"""

import os
import json
import time
from datetime import datetime, timezone
from pathlib import Path
from dataclasses import dataclass, asdict, field
from typing import Optional

import requests
import feedparser
import yfinance as yf
import praw
from dotenv import load_dotenv

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
load_dotenv()

GROQ_API_KEY    = os.getenv("GROQ_API_KEY")
FRED_API_KEY    = os.getenv("FRED_API_KEY")
REDDIT_ID       = os.getenv("REDDIT_CLIENT_ID")
REDDIT_SECRET   = os.getenv("REDDIT_CLIENT_SECRET")
REDDIT_AGENT    = os.getenv("REDDIT_USER_AGENT", "youtube-finance-bot:v1 (by /u/your_user)")

GROQ_MODEL      = "llama-3.1-8b-instant"
GROQ_ENDPOINT   = "https://api.groq.com/openai/v1/chat/completions"

OUTPUT_DIR      = Path("data")
OUTPUT_DIR.mkdir(exist_ok=True)

TODAY           = datetime.now(timezone.utc).strftime("%Y-%m-%d")


# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------
@dataclass
class Signal:
    """Una senal cruda de cualquier fuente."""
    source: str
    headline: str
    detail: str = ""
    url: str = ""

@dataclass
class VideoIdea:
    title: str
    hook: str
    angle: str
    target_keywords: list[str] = field(default_factory=list)
    source_signals: list[str] = field(default_factory=list)
    estimated_length_min: int = 8


# ---------------------------------------------------------------------------
# Fuente 1: RSS de noticias financieras
# ---------------------------------------------------------------------------
RSS_FEEDS = {
    "Reuters Business":   "https://www.reutersagency.com/feed/?best-sectors=business-finance&post_type=best",
    "Yahoo Finance":      "https://finance.yahoo.com/news/rssindex",
    "MarketWatch Top":    "https://www.marketwatch.com/rss/topstories",
    "CNBC Finance":       "https://www.cnbc.com/id/10000664/device/rss/rss.html",
    "Investing.com":      "https://www.investing.com/rss/news.rss",
}

def fetch_rss_headlines(max_per_feed: int = 5) -> list[Signal]:
    out: list[Signal] = []
    for name, url in RSS_FEEDS.items():
        try:
            feed = feedparser.parse(url)
            for entry in feed.entries[:max_per_feed]:
                out.append(Signal(
                    source   = f"RSS · {name}",
                    headline = entry.get("title", "").strip(),
                    detail   = entry.get("summary", "")[:300].strip(),
                    url      = entry.get("link", ""),
                ))
        except Exception as e:
            print(f"   ! RSS {name} fallo: {e}")
    return out


# ---------------------------------------------------------------------------
# Fuente 2: snapshot de mercado con yfinance
# ---------------------------------------------------------------------------
MARKET_TICKERS = {
    "S&P 500":    "^GSPC",
    "Nasdaq 100": "^NDX",
    "Dow Jones":  "^DJI",
    "VIX":        "^VIX",
    "Oro":        "GC=F",
    "Petroleo":   "CL=F",
    "Bitcoin":    "BTC-USD",
    "Ethereum":   "ETH-USD",
    "EUR/USD":    "EURUSD=X",
    "10Y Yield":  "^TNX",
}

def fetch_market_snapshot() -> list[Signal]:
    out: list[Signal] = []
    for name, ticker in MARKET_TICKERS.items():
        try:
            data = yf.Ticker(ticker).history(period="5d")
            if data.empty or len(data) < 2:
                continue
            last  = float(data["Close"].iloc[-1])
            prev  = float(data["Close"].iloc[-2])
            chg   = (last - prev) / prev * 100
            arrow = "↑" if chg >= 0 else "↓"
            out.append(Signal(
                source   = "Mercado · yfinance",
                headline = f"{name}: {last:,.2f} ({arrow}{abs(chg):.2f}% vs ayer)",
                detail   = f"ticker={ticker}",
            ))
        except Exception as e:
            print(f"   ! yfinance {ticker} fallo: {e}")
    return out


# ---------------------------------------------------------------------------
# Fuente 3: indicadores economicos FRED
# ---------------------------------------------------------------------------
FRED_SERIES = {
    "Fed Funds Rate":          "DFF",
    "Inflacion CPI YoY":       "CPIAUCSL",
    "Paro USA":                "UNRATE",
    "Mortgage 30y":            "MORTGAGE30US",
    "Sentimiento consumidor":  "UMCSENT",
}

def fetch_fred_indicators() -> list[Signal]:
    if not FRED_API_KEY:
        print("   · FRED_API_KEY no configurada — salto FRED.")
        return []
    out: list[Signal] = []
    for name, series in FRED_SERIES.items():
        try:
            r = requests.get(
                "https://api.stlouisfed.org/fred/series/observations",
                params={
                    "series_id":    series,
                    "api_key":      FRED_API_KEY,
                    "file_type":    "json",
                    "sort_order":   "desc",
                    "limit":        2,
                },
                timeout=10,
            )
            r.raise_for_status()
            obs = r.json().get("observations", [])
            if not obs:
                continue
            latest, *_ = obs
            out.append(Signal(
                source   = "Macro · FRED",
                headline = f"{name}: {latest['value']} (a fecha {latest['date']})",
                detail   = f"series={series}",
            ))
        except Exception as e:
            print(f"   ! FRED {series} fallo: {e}")
    return out


# ---------------------------------------------------------------------------
# Fuente 4: Reddit (que pregunta de verdad la gente)
# ---------------------------------------------------------------------------
SUBREDDITS = ["personalfinance", "investing", "financialindependence",
              "Bogleheads", "wallstreetbets", "CryptoCurrency"]

def fetch_reddit_hot(limit: int = 5) -> list[Signal]:
    if not (REDDIT_ID and REDDIT_SECRET):
        print("   · Credenciales Reddit no configuradas — salto Reddit.")
        return []
    try:
        reddit = praw.Reddit(
            client_id     = REDDIT_ID,
            client_secret = REDDIT_SECRET,
            user_agent    = REDDIT_AGENT,
        )
        reddit.read_only = True
    except Exception as e:
        print(f"   ! Reddit init fallo: {e}")
        return []

    out: list[Signal] = []
    for sub in SUBREDDITS:
        try:
            for post in reddit.subreddit(sub).hot(limit=limit):
                if post.stickied:
                    continue
                out.append(Signal(
                    source   = f"Reddit · r/{sub}",
                    headline = post.title.strip(),
                    detail   = f"score={post.score}, comentarios={post.num_comments}",
                    url      = f"https://reddit.com{post.permalink}",
                ))
        except Exception as e:
            print(f"   ! Reddit r/{sub} fallo: {e}")
    return out


# ---------------------------------------------------------------------------
# Generador de ideas con Groq
# ---------------------------------------------------------------------------
IDEA_GENERATOR_PROMPT = """You are the creative director of a faceless, automated YouTube channel about PERSONAL FINANCE and INVESTING for an English-speaking, mostly US/UK audience.

=== STRICT NICHE BOUNDARIES ===
IN-NICHE topics ONLY (everything else is OUT):
  - Personal finance (saving, budgeting, debt, credit, banking, taxes)
  - Investing (stocks, ETFs, index funds, bonds, dividends, portfolios)
  - Markets (S&P, Nasdaq, Dow, sector moves, individual tickers with money angle)
  - Macro (Fed, rates, inflation, jobs, GDP — only when explained as "what this means for your money")
  - Retirement (401k, IRA, pensions, FIRE movement)
  - Real estate as investment (mortgages, rentals, REITs)
  - Crypto as financial asset (BTC/ETH price, regulation, investment thesis)
  - Side income, passive income, financial independence

OUT OF NICHE — DO NOT propose these even if signals mention them:
  - Healthcare/medical topics (even if money saving is involved)
  - Pure tech news without an investment angle (e.g. "new iPhone features")
  - Politics without a direct money/markets impact
  - Lifestyle, productivity, self-help that isn't strictly about money
  - General business news without an actionable investing/finance angle

=== YOUR TASK ===
From the signals below, pick 5 distinct video angles. Each must:
  - Anchor in 1-3 real signals you see below (cite them verbatim in source_signals)
  - Have a clear MONEY ANGLE in the angle field — answer "how does this affect the viewer's wallet/portfolio?"
  - Be currently relevant (today/this week — no evergreen "what is a 401k")
  - Be high-retention (curiosity gap, contrast, urgency, specific numbers)
  - Be mid-length-friendly (8-12 min long video, later cut into 8-10 Shorts)
  - NOT be financial advice (educational tone — "what this means", "how this works", never "buy this stock")
  - Rotate format across the 5 ideas — mix at least 3 different formats from:
      news-breakdown, data-deep-dive, myth-buster, contrarian-take, case-study,
      comparison, "if-you-have-$X-today" scenario, historical-parallel, behavioral-psychology

=== HOOK RULES ===
Each hook must do ONE of these in 3-5 seconds:
  a) Promise a specific dollar amount or % ("This Fed move could cost you $4,200 a year")
  b) Set up a sharp contradiction or surprise ("Everyone is wrong about this market")
  c) Personal-stakes question ("Is your 401k about to lose 20%?")
  d) Insider/secret framing ("Wall Street is quietly doing X")
BANNED hooks: "In this video we'll...", "Today we're talking about...", anything generic.

=== OUTPUT FORMAT ===
Return JSON only. Shape: {{"ideas": [...5 objects...]}}
Each object MUST have these exact fields:
  - title: English, max 60 chars, CTR-optimized (specific number or curiosity gap)
  - hook: 3-5 second opening line in English, following the hook rules above
  - angle: 2-3 sentences explaining (a) the unique angle, (b) the explicit MONEY ANGLE — what's at stake for the viewer's money
  - target_keywords: list of 3-5 English SEO keywords
  - source_signals: list of verbatim source headlines you anchored on
  - estimated_length_min: integer 6-12

=== ANTI-FORCING RULE ===
   If a signal is out of niche (e.g. health, lifestyle, pure tech without investing angle), DO NOT try to bend it into finance by adding "what's the investing angle?" or similar patches. Simply ignore that signal and use others. Forcing a finance frame onto an unrelated news item produces low-quality clickbait and is FORBIDDEN.  

If you cannot find 5 strong in-niche ideas from the signals, return fewer (3 or 4) rather than forcing off-niche angles.

=== SIGNALS ===
{signals_text}
=== END SIGNALS ===
"""

def generate_ideas(signals: list[Signal]) -> list[VideoIdea]:
    if not GROQ_API_KEY:
        raise RuntimeError("Falta GROQ_API_KEY en .env")

    # Formateamos las senales en un bloque legible
    lines = []
    for s in signals:
        line = f"[{s.source}] {s.headline}"
        if s.detail:
            line += f" — {s.detail}"
        lines.append(line)
    signals_text = "\n".join(lines)

    payload = {
        "model":       GROQ_MODEL,
        "messages":    [{"role": "user",
                         "content": IDEA_GENERATOR_PROMPT.format(signals_text=signals_text)}],
        "temperature": 0.85,
        "max_tokens":  3000,
        "response_format": {"type": "json_object"},
    }

    # Groq a veces devuelve el array dentro de un objeto cuando pides JSON mode,
    # asi que pedimos un envoltorio simple: {"ideas": [...]}
    payload["messages"][0]["content"] += '\n\nReturn shape: {"ideas": [ ...5 objects... ]}'

    # Retry automatico con backoff exponencial para errores 429 y 5xx
    max_attempts = 4
    for attempt in range(1, max_attempts + 1):
        r = requests.post(
            GROQ_ENDPOINT,
            headers = {
                "Authorization": f"Bearer {GROQ_API_KEY}",
                "Content-Type":  "application/json",
            },
            json    = payload,
            timeout = 90,
        )
        if r.status_code == 200:
            break
        if r.status_code in (429, 500, 502, 503, 504) and attempt < max_attempts:
            wait = 30 * attempt
            print(f"   · Groq devolvio {r.status_code}, reintento en {wait}s (intento {attempt}/{max_attempts})...")
            time.sleep(wait)
            continue
        r.raise_for_status()

    content = r.json()["choices"][0]["message"]["content"]

    try:
        parsed = json.loads(content)
    except json.JSONDecodeError:
        # Plan B: intento de rescate buscando el primer '[' y ultimo ']'
        i, j = content.find("["), content.rfind("]")
        parsed = {"ideas": json.loads(content[i:j+1])} if i != -1 and j != -1 else {"ideas": []}

    raw_ideas = parsed.get("ideas") or parsed.get("videos") or []
    if isinstance(raw_ideas, dict):
        raw_ideas = list(raw_ideas.values())

    ideas: list[VideoIdea] = []
    for raw in raw_ideas:
        try:
            ideas.append(VideoIdea(
                title                = str(raw["title"])[:80],
                hook                 = str(raw["hook"]),
                angle                = str(raw["angle"]),
                target_keywords      = list(raw.get("target_keywords", []))[:5],
                source_signals       = list(raw.get("source_signals", []))[:5],
                estimated_length_min = int(raw.get("estimated_length_min", 8)),
            ))
        except (KeyError, ValueError, TypeError) as e:
            print(f"   ! Idea malformada, ignorada: {e}")
    return ideas


# ---------------------------------------------------------------------------
# Persistencia
# ---------------------------------------------------------------------------
def save_outputs(ideas: list[VideoIdea], signals: list[Signal]) -> tuple[Path, Path]:
    json_path = OUTPUT_DIR / f"ideas_{TODAY}.json"
    md_path   = OUTPUT_DIR / f"ideas_{TODAY}.md"

    json_path.write_text(json.dumps({
        "date":     TODAY,
        "n_signals": len(signals),
        "n_ideas":  len(ideas),
        "ideas":    [asdict(i) for i in ideas],
        "signals":  [asdict(s) for s in signals],
    }, indent=2, ensure_ascii=False), encoding="utf-8")

    md = [f"# Ideas de video — {TODAY}", "",
          f"_{len(signals)} senales agregadas · {len(ideas)} ideas generadas_", ""]
    for n, idea in enumerate(ideas, 1):
        md += [
            f"## {n}. {idea.title}",
            "",
            f"**Hook:** {idea.hook}",
            "",
            f"**Angulo:** {idea.angle}",
            "",
            f"**Keywords:** {', '.join(idea.target_keywords)}",
            "",
            f"**Duracion estimada:** {idea.estimated_length_min} min",
            "",
            "**Anclado en:**",
            *[f"- {s}" for s in idea.source_signals],
            "", "---", "",
        ]
    md_path.write_text("\n".join(md), encoding="utf-8")
    return json_path, md_path


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main() -> None:
    t0 = time.time()
    print(f"\n=== PIPELINE MATINAL · {TODAY} ===\n")

    print("1/5  Leyendo RSS...");          rss     = fetch_rss_headlines()
    print(f"      -> {len(rss)} titulares")
    print("2/5  Snapshot de mercado...");  market  = fetch_market_snapshot()
    print(f"      -> {len(market)} tickers")
    print("3/5  Macro FRED...");           macro   = fetch_fred_indicators()
    print(f"      -> {len(macro)} indicadores")
    print("4/5  Reddit caliente...");      reddit  = fetch_reddit_hot()
    print(f"      -> {len(reddit)} posts")

    all_signals = rss + market + macro + reddit
    if not all_signals:
        raise RuntimeError("No se obtuvo ninguna senal — revisa conexion y claves.")

    print(f"\n5/5  Generando 5 ideas con Groq ({GROQ_MODEL})...")
    ideas = generate_ideas(all_signals)

    if not ideas:
        raise RuntimeError("Groq no devolvio ideas validas.")

    json_path, md_path = save_outputs(ideas, all_signals)

    print(f"\n  Listo en {time.time()-t0:.1f}s")
    print(f"     JSON:  {json_path}")
    print(f"     Resumen MD:  {md_path}\n")

    print("=== IDEAS DE HOY ===\n")
    for n, i in enumerate(ideas, 1):
        print(f"  {n}. {i.title}")
        print(f"     Hook: {i.hook}\n")


if __name__ == "__main__":
    main()
