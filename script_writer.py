"""
================================================================================
 script_writer.py - Etapa 2 del canal de YouTube automatico (v4)
================================================================================

 Que hace:
   1. Lee las ideas del dia y descarta las que repiten el historial del canal
   2. Construye una FICHA DE DATOS reales para la idea: senales del dia,
      mercado, FRED, texto de los articulos fuente y busqueda en Google
   3. Escribe el guion largo + 3 Shorts independientes con un GUION VISUAL:
      cada seccion dice que grafico / cifra / comparativa / titular se ve y
      en que frase exacta aparece
   4. JUEZ DE CALIDAD: puntua hook, especificidad, retencion, titulo,
      miniatura y Shorts. Si no llega al minimo, reescribe con las
      correcciones; si sigue sin llegar, prueba la siguiente idea. Si
      ninguna vale, NO se publica nada ese dia.
   5. FACT-CHECK: corrige frases con cifras que no estan en la ficha y
      elimina visuales con numeros no verificables
   6. Titulo y miniatura no pueden repetir videos anteriores
   7. Guarda script_YYYY-MM-DD.json (etapas siguientes) y .md (legible)

 Coste: 0 EUR (Gemini free tier)
================================================================================
"""

import json
import random
import re
import time
from datetime import datetime, timezone
from pathlib import Path

import requests
from dotenv import load_dotenv

import content_history as ch
import llm
from engagement import PLAYLISTS

load_dotenv()

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
WEB_BACKLINK    = "https://carterapasiva.com"

INPUT_DIR       = Path("data")
OUTPUT_DIR      = Path("data")
OUTPUT_DIR.mkdir(exist_ok=True)

TODAY           = datetime.now(timezone.utc).strftime("%Y-%m-%d")

# Rangos esperados por tipo de seccion (min, max ideal)
SECTION_TARGETS = {
    "hook":  (15,  35),
    "intro": (45,  80),
    "body":  (200, 280),
    "outro": (60,  110),
}
SHORT_WORDS = (95, 150)

# Tolerancia: solo reintentamos si una seccion esta MUY por debajo (<60% del minimo)
TOLERANCE_RATIO      = 0.60
MIN_TOTAL_WORDS      = 1100
MAX_RETRY_GENERATION = 2
MAX_METADATA_RETRIES = 3

# Juez de calidad
QUALITY_THRESHOLD    = 7.5   # nota para aceptar a la primera
MIN_PUBLISH_SCORE    = 6.5   # nota que se busca antes de publicar
MAX_IDEAS_TO_TRY     = 3
MAX_REWRITES         = 3     # reescrituras por idea con las correcciones del juez
# Presupuesto de tiempo: se reescribe y se prueban ideas (dando vueltas a la lista) hasta llegar a la nota
# o agotarlo. Nunca un dia sin video: si nada llega a MIN_PUBLISH_SCORE, se publica el mejor guion.
STAGE_BUDGET_SEC     = 60 * 60
_T0                  = time.time()


def over_budget(margin_sec: int = 0) -> bool:
    return time.time() - _T0 > STAGE_BUDGET_SEC - margin_sec

VISUAL_TYPES = {"chart", "fred", "stat", "compare", "headline"}

# Series de FRED que el motor visual sabe pintar (id -> (etiqueta, unidades FRED))
FRED_CHARTS = {
    "MORTGAGE30US": ("30-year mortgage rate (%)", "lin"),
    "DGS10":        ("10-year Treasury yield (%)", "lin"),
    "DFF":          ("Fed funds rate (%)", "lin"),
    "CPIAUCSL":     ("Inflation, CPI year-over-year (%)", "pc1"),
    "UNRATE":       ("US unemployment rate (%)", "lin"),
    "UMCSENT":      ("Consumer sentiment index", "lin"),
    "PSAVERT":      ("Personal saving rate (%)", "lin"),
    "CSUSHPINSA":   ("US home prices, YoY change (%)", "pc1"),
    "GASREGW":      ("US regular gas price ($/gal)", "lin"),
    "T10Y2Y":       ("10Y minus 2Y Treasury spread (%)", "lin"),
    "REVOLSL":      ("US credit card debt ($ billions)", "lin"),
    "MSPUS":        ("Median US home sale price ($)", "lin"),
}

# Estilos de titulo que rotan (el aprendizaje favorece los que mejor rinden)
TITLE_STYLES = [
    "NAME-FIRST: open with the specific company/person/institution/asset name, then the stake (e.g. 'Barclays Cut 5,000 Jobs. Your Bank Is Next')",
    "NUMBER-FIRST: open with a specific number or dollar figure from the story (e.g. '$42,000: The Real Cost of Paying Off a 2.75% Mortgage')",
    "DIRECT QUESTION to the viewer about their money (e.g. 'Is Your 401k Quietly Betting on One Stock?')",
    "VERSUS: compare two things head to head (e.g. 'T-Bills vs High-Yield Savings: One Pays 40% More')",
    "BOLD CONTRARIAN CLAIM stated as fact (e.g. 'Lower Rates Will Make Housing Less Affordable')",
    "HOW IT WORKS: 'How X Actually ...' revealing a mechanism (e.g. 'How Buybacks Quietly Inflate Your EPS')",
    "TIMEFRAME WARNING: a deadline or date the viewer must care about (e.g. 'Before January 1st, Check This in Your IRA')",
    "THE STORY: 'The ... That ...' naming the subject (e.g. 'The $52B Loophole That Moved Tech Stocks')",
]


# ---------------------------------------------------------------------------
# Ideas
# ---------------------------------------------------------------------------
def load_ideas_payload() -> dict:
    path = INPUT_DIR / f"ideas_{TODAY}.json"
    if not path.exists():
        candidates = sorted(INPUT_DIR.glob("ideas_*.json"), reverse=True)
        if not candidates:
            raise FileNotFoundError("No hay ideas_*.json. Ejecuta morning_pipeline.py.")
        path = candidates[0]
        print(f"   . Usando JSON mas reciente: {path.name}")
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not payload.get("ideas"):
        raise ValueError(f"El archivo {path.name} no contiene ideas.")
    return payload


def fresh_ideas(ideas: list[dict], history: list[dict]) -> list[dict]:
    out = []
    for n, idea in enumerate(ideas, 1):
        dup = ch.find_duplicate(idea, history, TODAY)
        if dup:
            print(f"   x idea #{n} '{idea.get('title')}' repetida: {dup[1]}")
        else:
            out.append(idea)
    if not out:
        raise RuntimeError("Todas las ideas del dia repiten videos ya publicados.")
    return out


def pick_title_style(history: list[dict]) -> str:
    recent = {s for s in ch.recent_values(history, "title_style", last_n=3)}
    return ch.weighted_choice(TITLE_STYLES, history, "title_style",
                              exclude={t for t in TITLE_STYLES if t.split(":")[0] in recent})


# ---------------------------------------------------------------------------
# Ficha de datos (lo unico de donde pueden salir cifras)
# ---------------------------------------------------------------------------
def fetch_article_text(url: str, max_chars: int = 3500) -> str:
    try:
        r = requests.get(url, timeout=15, headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/130"})
        if r.status_code != 200 or "html" not in r.headers.get("content-type", ""):
            return ""
    except requests.RequestException:
        return ""
    paragraphs = re.findall(r"<p[^>]*>(.*?)</p>", r.text, re.S | re.I)
    text = " ".join(re.sub(r"<[^>]+>", "", p) for p in paragraphs)
    text = re.sub(r"\s+", " ", text).strip()
    return text[:max_chars]


def build_fact_sheet(idea: dict, payload: dict) -> str:
    signals = payload.get("signals", [])
    used = {ch.normalize(h) for h in idea.get("source_signals", [])}

    lines = [f"TODAY: {TODAY}", "", "SOURCE HEADLINES FOR THIS STORY:"]
    for s in signals:
        if ch.normalize(s.get("headline", "")) in used:
            lines.append(f"- [{s.get('source')}] {s.get('headline')} — {s.get('detail', '')}")

    lines += ["", "LIVE MARKET + MACRO DATA (real, today):"]
    for s in signals:
        if s.get("source", "").startswith(("Mercado", "Macro")):
            lines.append(f"- {s.get('headline')}")

    for url in idea.get("source_urls", [])[:3]:
        text = fetch_article_text(url)
        if text:
            lines += ["", f"ARTICLE TEXT ({url}):", text]

    print("   . buscando datos verificados con Google Search...")
    research = llm.research(
        topic=f"{idea.get('title')} — {idea.get('topic_key', '')}",
        context=idea.get("angle", "") + " Sources: " + "; ".join(idea.get("source_signals", [])),
    )
    if research:
        lines += ["", "WEB RESEARCH (Google Search, with sources):", research]
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Conteo de palabras y validacion
# ---------------------------------------------------------------------------
def count_words(text: str) -> int:
    return len(re.findall(r"\b\w+\b", text or ""))


def check_section(section_type: str, word_count: int) -> tuple[bool, int, int]:
    """(is_too_short, min_target, max_target); 'too_short' = <60% del minimo."""
    if section_type not in SECTION_TARGETS:
        return (False, 0, 0)
    min_w, max_w = SECTION_TARGETS[section_type]
    return (word_count < int(min_w * TOLERANCE_RATIO), min_w, max_w)


def measure_script(raw: dict) -> dict:
    per_section, short_sections, total = [], [], 0
    for i, s in enumerate(raw.get("sections", [])):
        stype = s.get("type", "body")
        w = count_words(s.get("text", ""))
        too_short, min_w, max_w = check_section(stype, w)
        per_section.append({"i": i, "type": stype, "words": w, "min": min_w,
                            "max": max_w, "too_short": too_short})
        total += w
        if too_short:
            short_sections.append((i, stype, w, min_w))
    return {
        "total_words":    total,
        "per_section":    per_section,
        "short_sections": short_sections,
        "is_too_short":   bool(short_sections) or total < MIN_TOTAL_WORDS,
    }


def format_breakdown(stats: dict) -> str:
    lines = []
    for ps in stats["per_section"]:
        if ps["min"] == 0:
            continue
        mark = f"  <-- SHORT, expand to {ps['min']}-{ps['max']}" if ps["too_short"] else ""
        lines.append(f"  Section {ps['i']+1} ({ps['type']}): {ps['words']} words (target {ps['min']}-{ps['max']}){mark}")
    lines.append(f"  TOTAL: {stats['total_words']} words (minimum {MIN_TOTAL_WORDS})")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Prompts
# ---------------------------------------------------------------------------
SCRIPT_WRITER_PROMPT = """You are the head writer of a fast-growing faceless finance YouTube channel. You write like the best
explainer creators: concrete, punchy, every sentence earns the next. The video is built from REAL DATA
VISUALS (charts, big numbers, comparisons, headline cards) synced to your narration, so you also direct
what appears on screen.

=== TODAY'S TOPIC (THE WHOLE VIDEO MUST BE ABOUT THIS) ===
Title idea: {idea_title}
Hook idea:  {idea_hook}
Angle:      {idea_angle}
Format:     {idea_format}
Keywords:   {idea_keywords}
Suggested tickers to chart: {idea_tickers}

=== FACT SHEET (the ONLY allowed source of numbers, dates and names) ===
{fact_sheet}
=== END FACT SHEET ===

FACT RULES (critical - this is a finance channel, credibility is everything):
- Every number, percentage, date and dollar figure you write MUST appear in the fact sheet above.
- If you need a number that is not in the fact sheet, describe it qualitatively instead. Never invent.
- Attribute claims ("according to CNBC...", "Fed data shows...").

=== WHAT THIS AUDIENCE RESPONDS TO (real channel analytics) ===
{performance_block}

=== ALREADY PUBLISHED - DO NOT REUSE THESE TITLES, THUMBNAILS OR ANGLES ===
{history_block}

=== TONE ===
- Talk to ONE viewer ("you"), like a sharp friend who works on Wall Street explaining it over coffee.
- No "In this video", "Let's dive in", "buckle up", "game-changer", "here's the thing", "let that sink in".
- Short sentences. Vary rhythm. Concrete over abstract. Always connect to the viewer's wallet.
- Educational, not financial advice. Never say "buy" or "sell" this.
- Write for the ear (TTS will read it): spell things the way they are said, no bullet points, no emojis,
  no parentheses, no abbreviations a voice would stumble on.

=== STRUCTURE (retention engine) ===
1. HOOK (15-35 words): the most surprising TRUE fact of the story + why the viewer's money is involved.
   The first sentence must work with the sound off as on-screen text. No greeting, no channel name.
2. INTRO (45-80 words): stakes for the viewer + an open loop ("by the end you'll know ...") that is only
   paid off in the last body section.
3. BODY (4 sections, 200-280 words each, each with a short punchy "title"): each one escalates.
   Use pattern interrupts every ~40 seconds (a counter-intuitive fact, a mini-story with a real name,
   a "but here's what nobody priced in"). End each body section with a tease of the next one.
4. OUTRO (60-110 words): pay off the open loop, ONE concrete thing the viewer can check or do today,
   a one-line disclaimer ("this is education, not financial advice"), then a CTA that gives a reason
   to subscribe tied to the topic (not "like and subscribe").

=== ON-SCREEN VISUALS (each section gets a "visuals" list) ===
Visual types (use ONLY these):
  {{"type": "chart", "anchor": "...", "ticker": "COST", "period": "1y", "label": "Costco stock, last 12 months"}}
      period one of: 5d, 1mo, 6mo, 1y, 5y. Ticker = Yahoo Finance symbol (stocks, ETFs, ^GSPC, ^IXIC, BTC-USD, GC=F, CL=F, EURUSD=X).
  {{"type": "fred", "anchor": "...", "series": "MORTGAGE30US", "period": "5y"}}
      series one of: {fred_list}
  {{"type": "stat", "anchor": "...", "value": "$65", "label": "New Costco membership fee", "direction": "up"}}
      direction: up | down | neutral. value MUST be copied from the fact sheet.
  {{"type": "compare", "anchor": "...", "title": "What $10,000 earns in a year", "items": [{{"label": "Big bank savings", "value": 0.4, "display": "$40"}}, {{"label": "High-yield savings", "value": 4.3, "display": "$430"}}]}}
      2-4 items; every display value MUST come from the fact sheet.
  {{"type": "headline", "anchor": "...", "source": "CNBC", "text": "Costco raises membership fee for first time since 2017"}}
      text = a real headline from the fact sheet.
"anchor" = 3-6 words copied EXACTLY from that section's text: the visual appears when the voice says them.
Per section: hook 1 (a stat or headline - the very first seconds must show something concrete),
intro 1, each body 2-3, outro 0-1. Use at least 2 chart/fred visuals in the whole video.

=== 3 STANDALONE SHORTS ("shorts" list) ===
Each Short is a separate 40-55 second vertical video (95-150 words) about ONE striking fact from this story:
- First sentence <= 12 words, a pattern-breaking claim with the number or the surprise in it. NEVER start
  with "According to", a source name, "Did you know", "Today", "In a recent" or "So": viewers swipe
  away in the first second. Put the source later in the Short.
- "hook_text": 2-5 words shown huge on screen during the first 2 seconds: the most striking number or
  claim (e.g. "$6.3B STOLEN", "BANKS WIN, YOU LOSE"). Not the title.
- One idea, one payoff. Last sentence loops back into the first so it rewatches naturally.
- Its own "title" and 2-3 "visuals" (same types/rules).
{short_title_rules}

=== YOUTUBE METADATA ===
- youtube_title: 40-60 chars about TODAY'S TOPIC, naming its specific subject.
  Title style for today: {title_style}
  Do NOT start the title with: {overused_openers}
- thumbnail_text: 2-4 punchy words specific to this story (a name, number or claim). Must not match any
  thumbnail listed above. It complements the title, it does not repeat it.
- description: 150-250 words. First 2 lines hook the reader (they show in search). Then what they'll learn,
  the sources used, a one-line "educational, not financial advice" disclaimer, and end with:
  "Read the Spanish-language deep dive at {web_backlink}"
- tags: 10-15 specific search phrases people actually type.
- pinned_comment: the channel's own comment under the video. ONE specific question (max 180 chars) about
  this story that viewers can answer from their own life or opinion (e.g. "Would you still renew at $65,
  or is Costco losing you?"). No links, no "like and subscribe", no emojis.
- playlist: exactly one of: {playlists}

=== OUTPUT ===
Return ONLY this JSON:
{{
  "youtube_title": "...",
  "thumbnail_text": "...",
  "description": "...",
  "tags": ["..."],
  "pinned_comment": "...",
  "playlist": "...",
  "sections": [
    {{"type": "hook",  "text": "...", "visuals": [...]}},
    {{"type": "intro", "text": "...", "visuals": [...]}},
    {{"type": "body",  "title": "...", "text": "...", "visuals": [...]}},
    {{"type": "body",  "title": "...", "text": "...", "visuals": [...]}},
    {{"type": "body",  "title": "...", "text": "...", "visuals": [...]}},
    {{"type": "body",  "title": "...", "text": "...", "visuals": [...]}},
    {{"type": "outro", "text": "...", "visuals": [...]}}
  ],
  "shorts": [
    {{"title": "...", "hook_text": "...", "text": "...", "visuals": [...]}},
    {{"title": "...", "hook_text": "...", "text": "...", "visuals": [...]}},
    {{"title": "...", "hook_text": "...", "text": "...", "visuals": [...]}}
  ]
}}
"""

EXPANSION_PROMPT = """Your previous draft was REJECTED because some sections were too short.

{previous_draft}

Word counts vs targets:
{word_breakdown}

Rewrite ONLY the sections marked SHORT to hit their target range (keep everything else identical,
including visuals and shorts). Add substance, not filler: another concrete example from the fact sheet,
a historical comparison, a second-order effect most people miss, or a misconception to correct.
Numbers must still come from this fact sheet:
{fact_sheet}

Return the FULL JSON in the same shape. Output ONLY the JSON.
"""

JUDGE_PROMPT = """You are a ruthless YouTube strategist who has grown finance channels to 1M subscribers.
Score this script package for a faceless finance channel. Be harsh: 7 means "good enough to compete",
9+ is rare. A generic script that could have been written any day scores <= 5.

Channel analytics (what this audience responds to):
{performance_block}

Recently published (originality reference):
{history_block}

SCRIPT PACKAGE:
{script}

Score 1-10 each:
- hook: would 70%+ of viewers still be watching after 30 seconds?
- specificity: real names, numbers, dates from a fresh story (not generic advice)?
- retention: escalation, open loops, pattern interrupts, teases between sections?
- title_ctr: would a US viewer browsing YouTube click this title?
- thumbnail: is thumbnail_text 2-4 words, punchy, and complementary to the title?
- originality: clearly different from the recently published list?
- shorts: do the 3 Shorts hook in the first sentence and loop?

Return ONLY JSON:
{{"scores": {{"hook": 0, "specificity": 0, "retention": 0, "title_ctr": 0, "thumbnail": 0, "originality": 0, "shorts": 0}},
  "overall": 0.0,
  "fixes": ["concrete, actionable fix 1", "fix 2", "..."]}}
"""

REWRITE_PROMPT = """A senior strategist reviewed your script package and scored it {overall}/10. Required fixes:
{fixes}

Previous package:
{previous_draft}

Rewrite the package applying EVERY fix. Keep the same JSON shape (sections with visuals, 3 shorts,
metadata) and the same length targets. Every number must still come from this fact sheet:
{fact_sheet}

Title style: {title_style}. Do NOT start the title with: {overused_openers}.
Return ONLY the JSON.
"""

FACT_CHECK_PROMPT = """You are a meticulous financial fact-checker. Compare every factual claim in the SCRIPT
against the FACT SHEET. A claim is UNSUPPORTED if it states a number, percentage, date, dollar amount,
or specific event that does not appear in (or directly follow from) the fact sheet.

FACT SHEET:
{fact_sheet}

SCRIPT (numbered texts):
{texts}

For each unsupported claim, give the exact original sentence and a corrected sentence that either uses
the right figure from the fact sheet or removes the specific number (keep the same meaning and tone,
written for voice-over). Do not touch supported sentences.

Return ONLY JSON: {{"issues": [{{"id": "S3", "original": "...", "fixed": "...", "reason": "..."}}]}}
Return {{"issues": []}} if everything is supported.
"""

METADATA_RETRY_PROMPT = """The YouTube title and/or thumbnail text below are too similar to videos this channel already published.

Video topic: {idea_title}
Angle: {idea_angle}
Rejected title: {bad_title}
Rejected thumbnail text: {bad_thumb}
Reason: {reason}

Already published (avoid anything similar):
{history_block}

Write a NEW title (40-60 chars, naming the specific subject of THIS story, style: {title_style})
and a NEW thumbnail_text (2-4 punchy words, specific to this story).
Return ONLY JSON: {{"youtube_title": "...", "thumbnail_text": "..."}}
"""


SHORTS_PROMPT = """Write 3 standalone YouTube Shorts (vertical, 40-55 seconds, 95-150 words each) based on
this video script. Each Short covers ONE striking fact from the story.
- First sentence <= 12 words, a pattern-breaking claim with the number or the surprise in it. NEVER start
  with "According to", a source name, "Did you know", "Today", "In a recent" or "So": viewers swipe
  away in the first second. Put the source later in the Short.
- "hook_text": 2-5 words shown huge on screen during the first 2 seconds: the most striking number or
  claim (e.g. "$6.3B STOLEN", "BANKS WIN, YOU LOSE"). Not the title.
- One idea, one payoff. The last sentence loops back into the first so it rewatches naturally.
- Every number must come from the fact sheet. Written for voice-over (no emojis, no parentheses).
{short_title_rules}
- Its own "title" and 2-3 "visuals" using ONLY these types:
  {{"type": "stat", "anchor": "3-6 exact words from the text", "value": "$5B", "label": "...", "direction": "up"}}
  {{"type": "chart", "anchor": "...", "ticker": "JPM", "period": "1y", "label": "..."}}
  {{"type": "headline", "anchor": "...", "source": "CNBC", "text": "a real headline from the fact sheet"}}

VIDEO TITLE: {title}
SCRIPT:
{script}

FACT SHEET:
{fact_sheet}

Return ONLY JSON: {{"shorts": [{{"title": "...", "hook_text": "...", "text": "...", "visuals": [...]}}, ...3 items]}}
"""

OUTRO_PROMPT = """This finance video script is missing its OUTRO. Write it (60-110 words, for voice-over):
pay off the open loop from the intro, give ONE concrete thing the viewer can check or do today, a one-line
disclaimer ("this is education, not financial advice"), then a CTA that gives a topic-specific reason to
subscribe (not "like and subscribe"). Only use numbers that appear in the script.

VIDEO TITLE: {title}
SCRIPT SO FAR:
{script}

Return ONLY JSON: {{"text": "..."}}
"""

OUTRO_FALLBACK = ("So that is the real story behind the headline. Before you react, check how much of this "
                  "already sits inside the funds and accounts you own. This is education, not financial advice. "
                  "If you want the next money story explained with real data before it hits the news, subscribe.")


def ensure_structure(raw: dict, ctx: dict) -> dict:
    """El modelo (o un JSON roto) puede perder el outro o los Shorts: se reponen aparte."""
    sections = raw.setdefault("sections", [])
    script_text = "\n\n".join(f"[{x.get('type', '').upper()}] {x.get('text', '')}" for x in sections)

    if sections and sections[-1].get("type") != "outro":
        print("   ! al guion le falta el outro: lo genero aparte")
        try:
            text = str(call_model(OUTRO_PROMPT.format(title=raw.get("youtube_title", ""), script=script_text),
                                  max_tokens=1000, smart=False).get("text", "")).strip()
        except (RuntimeError, llm.LLMError) as e:
            print(f"   ! fallo generando outro ({e}); uso el cierre por defecto")
            text = ""
        sections.append({"type": "outro", "text": text if count_words(text) >= 40 else OUTRO_FALLBACK, "visuals": []})

    if len(clean_shorts(raw)) < 3:
        print(f"   ! el guion trae {len(clean_shorts(raw))} Shorts validos: genero los Shorts aparte")
        try:
            new = call_model(SHORTS_PROMPT.format(title=raw.get("youtube_title", ""), script=script_text,
                                                  fact_sheet=ctx["fact_sheet"]), max_tokens=6000, smart=False)
            existing = [x for x in raw.get("shorts", []) or [] if isinstance(x, dict)]
            raw["shorts"] = existing + [x for x in new.get("shorts", []) if isinstance(x, dict)]
            print(f"   . Shorts tras regenerar: {len(clean_shorts(raw))}")
        except (RuntimeError, llm.LLMError) as e:
            print(f"   ! no pude generar los Shorts: {e}")
    return raw


# ---------------------------------------------------------------------------
# Llamadas al modelo
# ---------------------------------------------------------------------------
def call_model(prompt: str, max_tokens: int = 16000, temperature: float = 0.75, smart: bool = True) -> dict:
    """
    Devuelve el JSON parseado. smart=True usa el modelo bueno (cae al ligero si
    no hay cuota); el resto de tareas usan el ligero para no gastar su cuota diaria.
    """
    content = llm.generate(prompt, smart=smart, temperature=temperature, max_tokens=max_tokens)
    return _parse_gemini_response(content)


def _parse_gemini_response(content: str) -> dict:
    """Parsea la respuesta JSON de Gemini de forma tolerante."""
    try:
        parsed = llm.parse_json(content)
        if isinstance(parsed, dict):
            return parsed
    except llm.LLMError:
        pass

    print("   . JSON global roto, intentando rescate por campos...")
    rescued = _rescue_script_fields(content or "")
    if rescued and rescued.get("sections"):
        print(f"   . Rescate OK: {len(rescued.get('sections', []))} secciones recuperadas")
        return rescued

    print(f"   ! No se encontro JSON valido en la respuesta")
    print(f"     Contenido: '{(content or '')[:500]}'")
    raise RuntimeError("Respuesta de Gemini sin JSON valido")


def _rescue_script_fields(content: str) -> dict:
    """
    Ultimo recurso: si el JSON del guion esta malformado, extraer campo por campo
    con regex y balance de llaves. Recupera al menos las secciones del guion.
    """
    result = {}
    for field in ("youtube_title", "thumbnail_text", "description", "pinned_comment", "playlist"):
        m = re.search(rf'"{field}"\s*:\s*"((?:[^"\\]|\\.)*)"', content)
        if m:
            result[field] = m.group(1).replace('\\"', '"').replace('\\n', '\n').replace('\\\\', '\\')

    m = re.search(r'"tags"\s*:\s*\[([^\]]*)\]', content, re.DOTALL)
    if m:
        try:
            result["tags"] = json.loads("[" + m.group(1) + "]")
        except json.JSONDecodeError:
            result["tags"] = re.findall(r'"([^"\\]+)"', m.group(1))[:20]

    for key in ("sections", "shorts"):
        items = _rescue_array(content, key)
        if items:
            result[key] = items
    return result


def _rescue_array(content: str, key: str) -> list[dict]:
    """Objetos {...} completos (con "text") dentro de "key": [...], aunque el JSON este roto."""
    m = re.search(rf'"{key}"\s*:\s*\[', content)
    if not m:
        return []
    items, depth, obj_start = [], 0, -1
    in_string = escape = False
    i = m.end()
    while i < len(content):
        c = content[i]
        if escape:
            escape = False
        elif c == "\\":
            escape = True
        elif c == '"':
            in_string = not in_string
        elif not in_string:
            if c == "{":
                if depth == 0:
                    obj_start = i
                depth += 1
            elif c == "}":
                depth -= 1
                if depth == 0 and obj_start != -1:
                    obj_str = content[obj_start:i + 1]
                    for candidate in (obj_str, re.sub(r',\s*([}\]])', r'\1', obj_str)):
                        try:
                            obj = json.loads(candidate)
                            if "text" in obj:
                                items.append(obj)
                            break
                        except json.JSONDecodeError:
                            continue
                    obj_start = -1
            elif c == "]" and depth == 0:
                break
        i += 1
    return items

    sections, depth, obj_start = [], 0, -1
    in_string = escape = False
    i = sec_match.end()
    while i < len(content):
        c = content[i]
        if escape:
            escape = False
        elif c == "\\":
            escape = True
        elif c == '"':
            in_string = not in_string
        elif not in_string:
            if c == "{":
                if depth == 0:
                    obj_start = i
                depth += 1
            elif c == "}":
                depth -= 1
                if depth == 0 and obj_start != -1:
                    obj_str = content[obj_start:i + 1]
                    for candidate in (obj_str, re.sub(r',\s*([}\]])', r'\1', obj_str)):
                        try:
                            obj = json.loads(candidate)
                            if "text" in obj:
                                sections.append(obj)
                            break
                        except json.JSONDecodeError:
                            continue
                    obj_start = -1
            elif c == "]" and depth == 0:
                break
        i += 1

    if sections:
        result["sections"] = sections
    return result


# ---------------------------------------------------------------------------
# Generacion, juez y reescritura
# ---------------------------------------------------------------------------
def prompt_context(idea: dict, history: list[dict], title_style: str, fact_sheet: str) -> dict:
    overused = ch.overused_title_openers(history)
    return {
        "idea_title":        idea.get("title", ""),
        "idea_hook":         idea.get("hook", ""),
        "idea_angle":        idea.get("angle", ""),
        "idea_format":       idea.get("format", "") or "free",
        "idea_keywords":     ", ".join(idea.get("target_keywords", [])),
        "idea_tickers":      ", ".join(idea.get("tickers", [])) or "(none - use fred or index charts)",
        "fact_sheet":        fact_sheet,
        "performance_block": ch.performance_report(history),
        "history_block":     ch.recent_for_prompt(history, limit=40),
        "title_style":       title_style,
        "overused_openers":  ", ".join(f'"{w.capitalize()}"' for w in overused) or "(none)",
        "fred_list":         ", ".join(FRED_CHARTS),
        "web_backlink":      WEB_BACKLINK,
        "playlists":         " | ".join(PLAYLISTS),
    }


def generate_script(ctx: dict) -> dict:
    print("   . Pasada inicial...")
    raw = call_model(SCRIPT_WRITER_PROMPT.format(**ctx))
    stats = measure_script(raw)
    print(f"   . {stats['total_words']} palabras, {len(stats['short_sections'])} secciones muy cortas, "
          f"{len(raw.get('shorts', []))} shorts")

    for retry in range(1, MAX_RETRY_GENERATION + 1):
        if not stats["is_too_short"] or over_budget(margin_sec=8 * 60):
            break
        print(f"   . Reintento {retry}/{MAX_RETRY_GENERATION} con expansion quirurgica...")
        try:
            raw_new = call_model(EXPANSION_PROMPT.format(
                previous_draft=json.dumps(raw, indent=1, ensure_ascii=False),
                word_breakdown=format_breakdown(stats),
                fact_sheet=ctx["fact_sheet"],
            ), smart=False)
        except (RuntimeError, llm.LLMError) as e:
            print(f"   ! expansion fallo: {e}")
            break
        stats_new = measure_script(raw_new)
        if stats_new["total_words"] >= stats["total_words"] - 50 and raw_new.get("sections"):
            raw, stats = raw_new, stats_new
        print(f"   . Tras retry {retry}: {stats['total_words']} palabras")
    return raw


def package_for_judge(raw: dict) -> str:
    parts = [f"TITLE: {raw.get('youtube_title')}", f"THUMBNAIL TEXT: {raw.get('thumbnail_text')}", ""]
    for s in raw.get("sections", []):
        parts.append(f"[{s.get('type', '').upper()}{' - ' + s['title'] if s.get('title') else ''}] {s.get('text', '')}")
    for n, sh in enumerate(raw.get("shorts", []), 1):
        parts.append(f"[SHORT {n} - {sh.get('title', '')}] {sh.get('text', '')}")
    return "\n\n".join(parts)


def judge(raw: dict, ctx: dict) -> dict:
    try:
        verdict = llm.generate_json(JUDGE_PROMPT.format(
            performance_block=ctx["performance_block"],
            history_block=ctx["history_block"],
            script=package_for_judge(raw),
        ), temperature=0.2, max_tokens=2000)
    except llm.LLMError as e:
        print(f"   ! juez no disponible ({e}); acepto el guion sin nota")
        return {"overall": QUALITY_THRESHOLD, "scores": {}, "fixes": [], "unavailable": True}
    scores = verdict.get("scores", {})
    try:
        overall = float(verdict.get("overall") or 0)
    except (TypeError, ValueError):
        overall = 0.0
    if scores and not overall:
        overall = sum(float(v) for v in scores.values()) / len(scores)
    # Un hook o titulo flojo hunde el video aunque el resto sea bueno
    weakest = min((float(scores.get(k, 10)) for k in ("hook", "title_ctr", "specificity")), default=10)
    overall = round(min(overall, weakest + 1.5), 2)
    verdict["overall"] = overall
    print(f"   . Juez: {overall}/10  {json.dumps(scores)}")
    return verdict


def rewrite(raw: dict, verdict: dict, ctx: dict) -> dict:
    fixes = "\n".join(f"- {f}" for f in verdict.get("fixes", [])) or "- Make it more specific and gripping."
    return call_model(REWRITE_PROMPT.format(
        overall=verdict.get("overall"),
        fixes=fixes,
        previous_draft=json.dumps(raw, indent=1, ensure_ascii=False),
        fact_sheet=ctx["fact_sheet"],
        title_style=ctx["title_style"],
        overused_openers=ctx["overused_openers"],
    ))


def best_script_for_idea(idea: dict, payload: dict, history: list[dict]) -> tuple[dict, dict, dict]:
    """Devuelve (raw, verdict, ctx) del mejor intento para esta idea."""
    title_style = pick_title_style(history)
    print(f"   . estilo de titulo: {title_style.split(':')[0]}")
    fact_sheet = build_fact_sheet(idea, payload)
    print(f"   . ficha de datos: {len(fact_sheet)} caracteres")
    ctx = prompt_context(idea, history, title_style, fact_sheet)

    raw = generate_script(ctx)
    verdict = judge(raw, ctx)
    if verdict["overall"] >= QUALITY_THRESHOLD or verdict.get("unavailable"):
        return raw, verdict, ctx

    # Reescrituras con las correcciones del juez, siempre sobre la mejor version hasta ahora
    for n in range(1, MAX_REWRITES + 1):
        if verdict["overall"] >= QUALITY_THRESHOLD:
            break
        if over_budget(margin_sec=6 * 60):
            print("   . Sin tiempo para reescribir, me quedo con la mejor version")
            break
        print(f"   . Nota {verdict['overall']} < {QUALITY_THRESHOLD}: reescritura {n}/{MAX_REWRITES} con las correcciones del juez...")
        try:
            raw2 = rewrite(raw, verdict, ctx)
            if measure_script(raw2)["total_words"] < MIN_TOTAL_WORDS * 0.85:
                print("   ! la reescritura sale demasiado corta, se descarta")
                continue
            verdict2 = judge(raw2, ctx)
            if verdict2["overall"] >= verdict["overall"]:
                raw, verdict = raw2, verdict2
        except (RuntimeError, llm.LLMError) as e:
            print(f"   ! reescritura fallo: {e}")
    return raw, verdict, ctx


# ---------------------------------------------------------------------------
# Fact-check y validacion de visuales
# ---------------------------------------------------------------------------
def _numbers(text: str) -> set[str]:
    """Numeros normalizados: '$4.2B' -> {'4.2'}, '5,000' -> {'5000'}."""
    return {n.replace(",", "").rstrip(".") for n in re.findall(r"\d[\d,]*\.?\d*", text or "")}


def fact_check(raw: dict, fact_sheet: str) -> dict:
    texts = {}
    for i, s in enumerate(raw.get("sections", [])):
        texts[f"S{i+1}"] = s.get("text", "")
    for i, sh in enumerate(raw.get("shorts", [])):
        texts[f"SH{i+1}"] = sh.get("text", "")

    try:
        result = llm.generate_json(FACT_CHECK_PROMPT.format(
            fact_sheet=fact_sheet,
            texts="\n\n".join(f"[{k}] {v}" for k, v in texts.items()),
        ), temperature=0.1, max_tokens=4000)
        issues = result.get("issues", []) if isinstance(result, dict) else []
    except llm.LLMError as e:
        print(f"   ! fact-check no disponible: {e}")
        issues = []

    fixed = 0
    for issue in issues:
        orig, new = str(issue.get("original", "")).strip(), str(issue.get("fixed", "")).strip()
        if not orig or not new:
            continue
        for container in raw.get("sections", []) + raw.get("shorts", []):
            if orig in container.get("text", ""):
                container["text"] = container["text"].replace(orig, new)
                fixed += 1
                print(f"   . corregido: '{orig[:70]}...' ({issue.get('reason', '')[:60]})")
                break
    print(f"   . fact-check: {len(issues)} problemas detectados, {fixed} corregidos")

    # Visuales con cifras: cada numero tiene que estar en la ficha
    sheet_numbers = _numbers(fact_sheet)
    dropped = 0
    for container in raw.get("sections", []) + raw.get("shorts", []):
        kept = []
        for v in container.get("visuals", []) or []:
            if not isinstance(v, dict) or v.get("type") not in VISUAL_TYPES:
                dropped += 1
                continue
            if v["type"] == "stat":
                ok = _numbers(str(v.get("value", ""))) <= sheet_numbers and _numbers(str(v.get("value", "")))
            elif v["type"] == "compare":
                items = v.get("items", [])
                ok = 2 <= len(items) <= 5 and all(
                    _numbers(str(it.get("display", it.get("value", "")))) <= sheet_numbers for it in items)
            elif v["type"] == "fred":
                ok = v.get("series") in FRED_CHARTS
            elif v["type"] == "chart":
                ok = bool(re.fullmatch(r"[\^A-Z0-9.=\-]{1,12}", str(v.get("ticker", "")).upper()))
            else:
                ok = bool(v.get("text"))
            if ok:
                kept.append(v)
            else:
                dropped += 1
        container["visuals"] = kept
    if dropped:
        print(f"   . {dropped} visuales descartados (cifras no verificables o formato invalido)")
    return raw


# ---------------------------------------------------------------------------
# Metadata unica
# ---------------------------------------------------------------------------
def ensure_unique_metadata(raw: dict, idea: dict, history: list[dict], title_style: str) -> dict:
    """Si titulo o miniatura repiten un video anterior, regenera solo la metadata."""
    def _check(title: str, thumb: str):
        return ch.find_duplicate({
            "title":          title,
            "thumbnail_text": thumb,
            "topic_key":      idea.get("topic_key", ""),
            "entities":       idea.get("entities", []),
        }, history, TODAY)

    title = str(raw.get("youtube_title", "")).strip()
    thumb = str(raw.get("thumbnail_text", "")).strip()
    dup = _check(title, thumb) if title else (None, "titulo vacio")

    for attempt in range(1, MAX_METADATA_RETRIES + 1):
        if not dup:
            break
        print(f"   ! Metadata repetida ({dup[1]}). Regenerando titulo/miniatura {attempt}/{MAX_METADATA_RETRIES}...")
        try:
            new = call_model(METADATA_RETRY_PROMPT.format(
                idea_title    = idea.get("title", ""),
                idea_angle    = idea.get("angle", ""),
                bad_title     = title,
                bad_thumb     = thumb,
                reason        = dup[1],
                history_block = ch.recent_for_prompt(history, limit=40),
                title_style   = title_style,
            ), max_tokens=500, smart=False)
        except (RuntimeError, llm.LLMError) as e:
            print(f"   ! Fallo regenerando metadata: {e}")
            break
        title = str(new.get("youtube_title", title)).strip()
        thumb = str(new.get("thumbnail_text", thumb)).strip()
        dup = _check(title, thumb)

    if dup:
        # Ultimo recurso: el titulo de la idea ya paso el filtro anti-repeticion en la etapa 1
        print(f"   ! Sigue repetida, uso el titulo de la idea: '{idea.get('title')}'")
        title = idea.get("title", title)
        thumb = " ".join(title.upper().split()[:3])

    raw["youtube_title"]  = title[:100]
    raw["thumbnail_text"] = thumb
    return raw


# ---------------------------------------------------------------------------
# Normalizacion y persistencia
# ---------------------------------------------------------------------------
def clean_sections(raw: dict) -> list[dict]:
    out = []
    for s in raw.get("sections", []):
        text = str(s.get("text", "")).strip()
        if not text:
            continue
        out.append({
            "type":    str(s.get("type", "body")),
            "title":   str(s.get("title", "")),
            "text":    text,
            "visuals": [v for v in s.get("visuals", []) or [] if isinstance(v, dict)],
        })
    return out


WEAK_OPENER = re.compile(r"^(according to [^,]{1,60},|did you know( that)?|so,?|today,?|in a recent [^,]{1,60},)\s*", re.I)


def fix_opening(text: str) -> str:
    """Quita la coletilla floja del arranque ("According to CNBC, ..."): en TikTok la mayoria se iba en
    el segundo 1. La fuente se queda en el resto del Short."""
    fixed = WEAK_OPENER.sub("", text, count=1)
    return fixed[:1].upper() + fixed[1:] if fixed != text and fixed else text


# Titulos de los Shorts: en el canal los concretos y en segunda persona retienen 55-73 %; los abstractos sacados
# del nombre de una seccion ("The Anatomy of the Breach", "The First Year Pace") se quedaban en 22-51 % (nov-2026).
SHORT_TITLE_RULES = """- "title" (40-70 chars, no hashtags) is what makes a scrolling viewer stop. Make it CONCRETE: name the
  company, person or number, and either speak to the viewer ("you", "your money") or promise a clear answer
  ("Why...", "How..."). Good: "How Wall Street banks made $52B on AI without building it",
  "Your high-yield savings account is making you poorer", "The Fed just warned AI is coming for office jobs".
  NEVER an abstract label or chapter name like "The Anatomy of the Breach" or "The First Year Pace"."""
# Llamada a seguir/suscribirse al final de cada Short (el mismo video va a YouTube, Instagram y TikTok)
SHORT_CTA = "Follow for a new market story every day."
SCRIPT_WRITER_PROMPT = SCRIPT_WRITER_PROMPT.replace("{short_title_rules}", SHORT_TITLE_RULES)
SHORTS_PROMPT = SHORTS_PROMPT.replace("{short_title_rules}", SHORT_TITLE_RULES)
_CONCRETE = re.compile(r"\d|\$|%|\byou(r|'re)?\b|^(why|how|what|when|who)\b", re.I)


def short_title(title: str, text: str) -> str:
    """El titulo del Short si es concreto; si no, la primera frase del Short (el gancho, con su cifra)."""
    title = title.strip().strip('"')
    if title and _CONCRETE.search(title) and len(title) >= 25:
        return title[:90]
    first = re.split(r"(?<=[.!?])\s", text.strip(), maxsplit=1)[0].rstrip(".!? ")
    if len(first) > 75:
        first = first[:75].rsplit(" ", 1)[0]
    return first or title[:90]


def clean_shorts(raw: dict) -> list[dict]:
    out = []
    for sh in raw.get("shorts", []) or []:
        text = fix_opening(str(sh.get("text", "")).strip())
        if count_words(text) < SHORT_WORDS[0] * 0.6:
            continue
        if SHORT_CTA.lower() not in text.lower():
            text = text.rstrip() + " " + SHORT_CTA
        out.append({
            "title":     short_title(str(sh.get("title", "")), text),
            "hook_text": str(sh.get("hook_text", "")).strip()[:40],
            "text":      text,
            "visuals":   [v for v in sh.get("visuals", []) or [] if isinstance(v, dict)],
        })
    return out[:3]


def save_outputs(raw: dict, idea: dict, verdict: dict, title_style: str, fact_sheet: str) -> tuple[Path, Path]:
    json_path = OUTPUT_DIR / f"script_{TODAY}.json"
    md_path   = OUTPUT_DIR / f"script_{TODAY}.md"
    (OUTPUT_DIR / f"factsheet_{TODAY}.txt").write_text(fact_sheet, encoding="utf-8")

    sections, shorts = clean_sections(raw), clean_shorts(raw)
    real_words = sum(count_words(s["text"]) for s in sections)

    json_path.write_text(json.dumps({
        "date":            TODAY,
        "source_idea":     idea,
        "topic_key":       idea.get("topic_key", ""),
        "entities":        idea.get("entities", []),
        "format":          idea.get("format", ""),
        "title_style":     title_style.split(":")[0],
        "youtube_title":   raw.get("youtube_title", ""),
        "thumbnail_text":  raw.get("thumbnail_text", ""),
        "description":     str(raw.get("description", "")),
        "tags":            [str(t) for t in raw.get("tags", [])][:25],
        "pinned_comment":  str(raw.get("pinned_comment", "")).strip()[:300],
        "playlist":        raw.get("playlist") if raw.get("playlist") in PLAYLISTS else "Stock Market Explained",
        "word_count_real": real_words,
        "quality":         {"overall": verdict.get("overall"), "scores": verdict.get("scores", {}),
                            "fixes": verdict.get("fixes", [])},
        "sections":        sections,
        "shorts":          shorts,
    }, indent=2, ensure_ascii=False), encoding="utf-8")

    md = [f"# {raw.get('youtube_title')}", "",
          f"_{TODAY} . {real_words} words . quality {verdict.get('overall')}/10_", "",
          f"**Thumbnail text:** {raw.get('thumbnail_text')}", "",
          f"**Comment:** {raw.get('pinned_comment', '')}  ·  **Playlist:** {raw.get('playlist', '')}", "",
          "## Description", "",
          str(raw.get("description", "")), "", "## Script", ""]
    for i, s in enumerate(sections, 1):
        md += [f"### {i}. {s['type'].upper()}{' - ' + s['title'] if s['title'] else ''} "
               f"({count_words(s['text'])} words)", "", s["text"], ""]
        md += [f"> visual: `{json.dumps(v, ensure_ascii=False)}`" for v in s["visuals"]] + [""]
    for i, sh in enumerate(shorts, 1):
        md += [f"## Short {i}: {sh['title']}", "", sh["text"], ""]
    md_path.write_text("\n".join(md), encoding="utf-8")
    return json_path, md_path


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main() -> None:
    t0 = time.time()
    print(f"\n=== SCRIPT WRITER . {TODAY} ===\n")

    history = ch.load_history()
    print(f"1/4  Cargando ideas (historial: {len(history)} videos)...")
    payload = load_ideas_payload()
    candidates = fresh_ideas(payload["ideas"], history)[:MAX_IDEAS_TO_TRY]

    print(f"\n2/4  Guion + juez de calidad (modelo {llm.MODEL_SMART}, respaldo {llm.MODEL_FAST})...")
    best = None
    n = 0
    # Se dan vueltas a la lista de ideas (con guiones nuevos cada vez) hasta llegar a la nota minima o agotar
    # el presupuesto de tiempo; con la nota "de primera" se para en cuanto aparece
    while candidates:
        if best and best[1]["overall"] >= MIN_PUBLISH_SCORE and n >= len(candidates):
            break
        if best and over_budget(margin_sec=12 * 60):
            print("\n   . Presupuesto de tiempo agotado: no pruebo mas ideas")
            break
        idea = candidates[n % len(candidates)]
        n += 1
        print(f"\n   --- Intento {n} (idea {(n - 1) % len(candidates) + 1}/{len(candidates)}): '{idea.get('title')}' ---")
        try:
            raw, verdict, ctx = best_script_for_idea(idea, payload, history)
        except (RuntimeError, llm.LLMError) as e:
            print(f"   ! fallo con esta idea: {e}")
            if n >= 4 * len(candidates):
                break  # el modelo no responde: no se insiste para siempre
            continue
        if not best or verdict["overall"] > best[1]["overall"]:
            best = (raw, verdict, ctx, idea)
        if verdict["overall"] >= QUALITY_THRESHOLD:
            break

    if not best:
        raise RuntimeError("No se pudo generar ningun guion (el modelo no respondio).")
    raw, verdict, ctx, idea = best
    if verdict["overall"] < MIN_PUBLISH_SCORE:
        # Nunca un dia sin video: se publica el mejor guion conseguido y queda anotado
        print(f"   ! Ningun guion llego a {MIN_PUBLISH_SCORE} tras {n} intentos: publico el mejor ({verdict['overall']}/10)")

    print(f"\n3/4  Fact-check y metadata (idea elegida: '{idea.get('title')}', nota {verdict['overall']})...")
    raw = ensure_structure(raw, ctx)
    raw = fact_check(raw, ctx["fact_sheet"])
    raw = ensure_unique_metadata(raw, idea, history, ctx["title_style"])

    if not clean_sections(raw):
        raise RuntimeError("El guion no tiene secciones validas.")

    print("\n4/4  Guardando guion...")
    json_path, md_path = save_outputs(raw, idea, verdict, ctx["title_style"], ctx["fact_sheet"])

    sections, shorts = clean_sections(raw), clean_shorts(raw)
    total = sum(count_words(s["text"]) for s in sections)
    n_vis = sum(len(s["visuals"]) for s in sections)
    print(f"\n  Listo en {time.time()-t0:.1f}s")
    print(f"     JSON: {json_path}\n     MD:   {md_path}\n")
    print("=== RESUMEN DEL GUION ===\n")
    print(f"  Titulo:     {raw.get('youtube_title')}")
    print(f"  Miniatura:  {raw.get('thumbnail_text')}")
    print(f"  Calidad:    {verdict['overall']}/10")
    print(f"  Palabras:   {total} (~{total/155:.1f} min)")
    print(f"  Secciones:  {len(sections)} con {n_vis} visuales de datos")
    print(f"  Shorts:     {len(shorts)}")
    for i, s in enumerate(sections, 1):
        print(f"    {i}. {s['type']:6s} {count_words(s['text']):>4} palabras, {len(s['visuals'])} visuales")
    print()


if __name__ == "__main__":
    main()
