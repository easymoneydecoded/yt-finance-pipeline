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

GROQ_API_KEY    = os.getenv("GROQ_API_KEY")  # legacy, no usado
GEMINI_API_KEY  = os.getenv("GEMINI_API_KEY")
FRED_API_KEY    = os.getenv("FRED_API_KEY")
REDDIT_ID       = os.getenv("REDDIT_CLIENT_ID")
REDDIT_SECRET   = os.getenv("REDDIT_CLIENT_SECRET")
REDDIT_AGENT    = os.getenv("REDDIT_USER_AGENT", "youtube-finance-bot:v1 (by /u/your_user)")

GEMINI_MODEL    = "gemini-3.5-flash-lite"
GEMINI_ENDPOINT = f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent"

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
IDEA_GENERATOR_PROMPT = """You are the creative director of a faceless, automated YouTube channel about personal finance and investing for an English-speaking, mostly US/UK audience.

Below are real, fresh signals from today: financial news headlines, a live market snapshot, macro indicators, and what people are actively asking on Reddit. Use these as raw material.

Your job: pick 5 distinct video angles. Each should be:
  - **Currently relevant** (anchored in a real signal you can see below)
  - **High retention** (curiosity, contrast, urgency, money-on-the-line)
  - **Mid-length friendly** (8-12 min long video that we'll later cut into 8-10 Shorts)
  - **NOT generic** (avoid "how to budget", "what is a 401k" — those are evergreen blog topics; we need angles that feel like today)
  - **NOT financial advice** (educational tone only — "what this means", "how this works", never "buy this")
  - **Rotated in format** across the 5 ideas: mix at least 3 different formats from: news breakdown, data deep-dive, myth-buster, contrarian take, case study, comparison, "if you have $X today" scenario, behavioral psychology angle, historical parallel

For each idea give me strict JSON with:
  - title:        an English YouTube title, max 60 chars, CTR-optimized (curiosity gap or specific number)
  - hook:         3-5 second opening line in English. Must make someone STOP scrolling. No "in this video we will" garbage.
  - angle:        2-3 sentences explaining the unique angle and why it works NOW
  - target_keywords: list of 3-5 SEO keywords in English
  - source_signals: list of the source headlines (verbatim) you anchored this idea on
  - estimated_length_min: integer 6-12

Return ONLY a JSON array of 5 objects. No prose before or after, no markdown fences.

=== SIGNALS ===
{signals_text}
=== END SIGNALS ===
"""

def _call_gemini_with_retries(payload: dict, max_attempts: int = 7) -> str:
    """Llama a la API de Gemini con reintentos ante 429/5xx. Devuelve el texto de respuesta."""
    last_err = None
    for attempt in range(1, max_attempts + 1):
        try:
            r = requests.post(
                GEMINI_ENDPOINT,
                headers = {
                    "x-goog-api-key": GEMINI_API_KEY,
                    "Content-Type":   "application/json",
                },
                json    = payload,
                timeout = 120,
            )
            if r.status_code == 200:
                data = r.json()
                try:
                    return data["candidates"][0]["content"]["parts"][0]["text"]
                except (KeyError, IndexError) as e:
                    last_err = f"Respuesta Gemini sin texto: {e} / {str(data)[:200]}"
                    print(f"   ! {last_err}")
            elif r.status_code in (429, 500, 502, 503, 504):
                wait = 30 * attempt
                snippet = r.text[:200].replace("\n", " ")
                print(f"   . Gemini devolvio {r.status_code}: {snippet}")
                print(f"     reintento en {wait}s (intento {attempt}/{max_attempts})...")
                time.sleep(wait)
                continue
            else:
                # Otros errores: mostrar body y raise
                print(f"   ! Gemini error {r.status_code}: {r.text[:300]}")
                r.raise_for_status()
        except requests.exceptions.RequestException as e:
            last_err = str(e)
            wait = 30 * attempt
            print(f"   . Gemini error de red: {e}. Reintento en {wait}s...")
            time.sleep(wait)
    raise RuntimeError(f"Gemini fallo tras {max_attempts} intentos: {last_err}")


def _robust_parse_ideas(content: str) -> dict:
    """
    Parseo robusto que rescata ideas individuales aunque el JSON global este roto.
    Si el JSON esta truncado o malformado, intenta extraer los objetos {..} uno a uno.
    """
    content = (content or "").strip()

    # Limpiar markdown code blocks
    if content.startswith("```"):
        lines = content.split("\n")
        content = "\n".join(lines[1:])
        if content.rstrip().endswith("```"):
            content = content.rstrip()[:-3]
    content = content.strip()

    # Intento 1: parseo directo
    try:
        return json.loads(content)
    except json.JSONDecodeError:
        pass

    # Intento 2: buscar array [...] completo
    i, j = content.find("["), content.rfind("]")
    if i != -1 and j != -1 and j > i:
        try:
            return {"ideas": json.loads(content[i:j+1])}
        except json.JSONDecodeError:
            pass

    # Intento 3: buscar objeto {...} completo
    i, j = content.find("{"), content.rfind("}")
    if i != -1 and j != -1 and j > i:
        try:
            return json.loads(content[i:j+1])
        except json.JSONDecodeError:
            pass

    # Intento 4 (RESCATE): extraer objetos {...} uno a uno usando balance de llaves
    print("   . JSON global roto, rescatando ideas individuales...")
    ideas = []
    depth = 0
    start = -1
    in_string = False
    escape = False

    for idx, ch in enumerate(content):
        if escape:
            escape = False
            continue
        if ch == "\\":
            escape = True
            continue
        if ch == '"':
            in_string = not in_string
            continue
        if in_string:
            continue
        if ch == "{":
            if depth == 0:
                start = idx
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0 and start != -1:
                obj_str = content[start:idx+1]
                try:
                    obj = json.loads(obj_str)
                    # Solo ideas validas (tienen title y hook)
                    if "title" in obj and "hook" in obj:
                        ideas.append(obj)
                except json.JSONDecodeError:
                    pass  # objeto malformado, saltar
                start = -1

    print(f"   . Rescatadas {len(ideas)} ideas del JSON roto")
    return {"ideas": ideas}


def generate_ideas(signals: list[Signal]) -> list[VideoIdea]:
    if not GEMINI_API_KEY:
        raise RuntimeError("Falta GEMINI_API_KEY en .env")

    # Formateamos las senales en un bloque legible
    lines = []
    for s in signals:
        line = f"[{s.source}] {s.headline}"
        if s.detail:
            line += f" — {s.detail}"
        lines.append(line)
    signals_text = "\n".join(lines)

    prompt_text = IDEA_GENERATOR_PROMPT.format(signals_text=signals_text)
    prompt_text += '\n\nReturn shape: {"ideas": [ ...5 objects... ]}'

    payload = {
        "contents": [{
            "parts": [{"text": prompt_text}]
        }],
        "generationConfig": {
            "temperature":      0.85,
            "maxOutputTokens":  6000,
            "responseMimeType": "application/json",
        },
    }

    content = _call_gemini_with_retries(payload)

    parsed = _robust_parse_ideas(content)

    # Gemini puede devolver el array directamente [..] en vez de envuelto {"ideas": [..]}
    if isinstance(parsed, list):
        raw_ideas = parsed
    else:
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

    print(f"\n5/5  Generando 5 ideas con Gemini ({GEMINI_MODEL})...")
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
