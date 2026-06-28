"""
================================================================================
 script_writer.py — Etapa 2 del canal de YouTube automatico
================================================================================

 Que hace:
   1. Lee el JSON de ideas generadas por la etapa 1 (morning_pipeline.py)
   2. Coge la primera idea (la mejor, segun el ranking del prompt)
   3. Llama a Groq para escribir un guion completo y estructurado
   4. Guarda el guion en JSON (consumible por etapa 3) y en MD (legible)

 Estructura del guion:
   - Metadata YouTube (title, description, tags, thumbnail)
   - Hook de 3-5 segundos
   - Intro con promesa de valor (15-20s)
   - 4-6 secciones de cuerpo, cada una candidata a Short con hook propio
   - Retention buttons intercalados (frases que mantienen al espectador)
   - CTA final (suscribirse + siguiente video)

 Coste: 0 EUR (Groq free tier)

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
from dotenv import load_dotenv

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
load_dotenv()

GROQ_API_KEY    = os.getenv("GROQ_API_KEY")
GROQ_MODEL      = "llama-3.3-70b-versatile"
GROQ_ENDPOINT   = "https://api.groq.com/openai/v1/chat/completions"

WEB_BACKLINK    = "https://carterapasiva.com"  # Para SEO cruzado

INPUT_DIR       = Path("data")
OUTPUT_DIR      = Path("data")
OUTPUT_DIR.mkdir(exist_ok=True)

TODAY           = datetime.now(timezone.utc).strftime("%Y-%m-%d")


# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------
@dataclass
class ScriptSection:
    type:             str               # hook | intro | body | retention | outro
    text:             str               # Lo que el TTS va a leer
    title:            str = ""          # Solo para body sections
    estimated_sec:    int = 0
    shorts_candidate: bool = False      # Si esta seccion es candidata a Short
    shorts_alt_hook:  str = ""          # Hook alternativo si se usa como Short

@dataclass
class VideoScript:
    date:                  str
    source_idea_title:     str
    youtube_title:         str
    thumbnail_text:        str
    description:           str
    tags:                  list[str]
    estimated_duration_min: int
    word_count:            int
    sections:              list[ScriptSection] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Carga de la idea del dia
# ---------------------------------------------------------------------------
def load_todays_idea() -> dict:
    path = INPUT_DIR / f"ideas_{TODAY}.json"
    if not path.exists():
        # Buscar el JSON mas reciente como fallback
        candidates = sorted(INPUT_DIR.glob("ideas_*.json"), reverse=True)
        if not candidates:
            raise FileNotFoundError(
                "No hay ningun ideas_*.json. Ejecuta morning_pipeline.py primero."
            )
        path = candidates[0]
        print(f"   · Usando JSON mas reciente: {path.name}")

    payload = json.loads(path.read_text(encoding="utf-8"))
    ideas = payload.get("ideas", [])
    if not ideas:
        raise ValueError(f"El archivo {path.name} no contiene ideas.")
    print(f"   · Cargadas {len(ideas)} ideas del dia. Eligiendo la #1.")
    return ideas[0]


# ---------------------------------------------------------------------------
# Generacion del guion con Groq
# ---------------------------------------------------------------------------
SCRIPT_WRITER_PROMPT = """You are the head writer of a popular faceless YouTube channel about personal finance and investing for a US/UK audience. Your scripts compete with channels like Graham Stephan, The Plain Bagel, and Two Cents — conversational, specific, packed with concrete numbers.

=== INPUT IDEA ===
Title:    {idea_title}
Hook:     {idea_hook}
Angle:    {idea_angle}
Keywords: {idea_keywords}
Anchored signals:
{idea_signals}

=== TONE RULES (CRITICAL — these are what separate good scripts from AI slop) ===
DO:
  - Write like you're texting a smart friend who asked you about money. Direct, specific, occasionally blunt.
  - Use contractions (you're, it's, that's, here's, won't, can't).
  - Start sentences with conjunctions (And, But, So) when it sounds natural.
  - Use concrete numbers EVERYWHERE. Not "a lot of money" — "$4,200". Not "rates went up" — "rates jumped 0.5% in 6 weeks".
  - Give specific examples: "If you bought a house in Phoenix last year for $400k...", "Your aunt who has a savings account at Chase..."
  - Acknowledge counterarguments briefly: "Now, some economists disagree, but the data shows..."

DON'T (these are AI-voice tells that kill retention):
  - "In this video we'll explore" / "Let's dive in" / "Without further ado"
  - "It's pretty straightforward" / "It all starts with" / "So, what's behind this?"
  - "That's a big deal" / "That's a big difference" — show, don't tell.
  - Rhetorical questions inside body sections ("But what does this mean?" — the viewer will leave). Save them for hooks.
  - Vague hedges: "could", "might", "potentially". Be direct: "will", "does", "is".

=== STRUCTURE (in this exact order) ===

1. **hook** — 12-20 words. Must stop the scroll. Pick ONE of these patterns:
     a) Specific personal stakes: "If you have a mortgage, the next 60 seconds could save you $300 a month."
     b) Sharp contradiction: "Everyone is wrong about the Fed's next move. Here's what's actually about to happen."
     c) Insider/secret framing: "Wall Street is quietly buying something most retail investors haven't even heard of."
     d) Cost-of-inaction: "Doing nothing about this Fed decision will cost the average homeowner $4,200 this year."
   NEVER start with "This Fed move could cost you" — that's the idea's hook, you need to ELEVATE it.

2. **intro** — 40-60 words (~15-20 sec spoken). Establish: what's happening + why now + what specifically the viewer will learn. End on a curiosity gap that pays off later in the video. Reference a real event/name/number from the anchored signals.

3. **body** — Exactly 4 sections, each 200-280 words (~80-100 sec spoken). Each body section MUST:
     - Open with a mini-hook sentence (NOT "So, here's the deal" — a specific claim or number)
     - Contain at least TWO specific numbers OR a worked example with real figures
     - End with a transition that pulls into the next section (not a summary)
     - Mark the 3 strongest as shorts_candidate: true
     - For Shorts candidates, write a shorts_alt_hook (8-15 words) that works as a 3-second standalone opener

4. **retention** — 2 sections of 30-45 words each, placed between body sections 2-3 and 3-4. Each retention button must TEASE a SPECIFIC payoff in the next section, not a generic "it gets interesting". Example: "But the part that surprised me most is what this does to your 401k — that's next."

5. **outro** — 80-120 words. Structure: (1) one-sentence recap of the single biggest takeaway, (2) specific actionable next step, (3) disclaimer line "This is for education, not financial advice — talk to a fiduciary for your situation", (4) specific CTA: "If this changed how you think about [topic], hit subscribe — I break down one of these every week."

=== HARD WORD COUNT REQUIREMENT ===
TOTAL spoken words across all sections combined MUST be between 1500 and 1800.
Distribution (use as floor, not ceiling):
  - hook:        12-20 words
  - intro:       40-60 words
  - each body:   200-280 words  (4 sections = 800-1120)
  - each retent: 30-45 words   (2 sections = 60-90)
  - outro:       80-120 words

If your draft is under 1500 total, ADD detail to body sections — more examples, more numbers, a brief historical comparison. Do NOT pad with filler.

=== YOUTUBE METADATA ===
  - youtube_title: max 60 chars. Sentence case (first letter capitalized, rest lowercase except proper nouns/acronyms). CTR-optimized.
  - thumbnail_text: 2-4 BIG punchy words. Example: "RATE HIKE INCOMING" not "Rate Hike". All caps is fine here.
  - description: 150-250 words. First 2 lines hook the reader. Include the disclaimer. End with: "Read the Spanish-language deep dive at {web_backlink}"
  - tags: 15-20 SEO tags, lowercase, mix broad + specific.

=== STRUCTURAL DISCIPLINE (READ TWICE) ===
The 4 body sections must each cover a DIFFERENT angle of the topic. Never let the last body section drift into "conclusion" or "summary" territory — that's the outro's job. If body section 4 starts with "In conclusion" or repeats CTAs/disclaimers, you've failed.

Body section 4 should introduce a FRESH ANGLE — typically the actionable response, the contrarian view, the historical parallel, or the surprising second-order effect. It must contain new information not covered in sections 1-3.

The outro is the ONLY place where: (a) the summary appears, (b) the CTA appears, (c) the disclaimer appears. Body sections never include these.

=== OUTPUT ===
Return ONE JSON object with these EXACT top-level fields:
{{
  "youtube_title": "...",
  "thumbnail_text": "...",
  "description": "...",
  "tags": ["...", "..."],
  "estimated_duration_min": 10,
  "word_count": <ACTUAL word count of all section texts combined>,
  "sections": [
    {{ "type": "hook",      "text": "...", "estimated_sec": 5 }},
    {{ "type": "intro",     "text": "...", "estimated_sec": 20 }},
    {{ "type": "body",      "title": "...", "text": "...", "estimated_sec": 90, "shorts_candidate": true, "shorts_alt_hook": "..." }},
    {{ "type": "body",      "title": "...", "text": "...", "estimated_sec": 90, "shorts_candidate": true, "shorts_alt_hook": "..." }},
    {{ "type": "retention", "text": "...", "estimated_sec": 15 }},
    {{ "type": "body",      "title": "...", "text": "...", "estimated_sec": 90, "shorts_candidate": true, "shorts_alt_hook": "..." }},
    {{ "type": "retention", "text": "...", "estimated_sec": 15 }},
    {{ "type": "body",      "title": "...", "text": "...", "estimated_sec": 90, "shorts_candidate": false }},
    {{ "type": "outro",     "text": "...", "estimated_sec": 30 }}
  ]
}}

Output ONLY the JSON. No prose before or after. No markdown fences.
The word_count field MUST be the real count of all section text fields combined.
"""


def generate_script(idea: dict) -> dict:
    if not GROQ_API_KEY:
        raise RuntimeError("Falta GROQ_API_KEY")

    signals_text = "\n".join(f"  - {s}" for s in idea.get("source_signals", []))

    prompt = SCRIPT_WRITER_PROMPT.format(
        idea_title    = idea.get("title", ""),
        idea_hook     = idea.get("hook", ""),
        idea_angle    = idea.get("angle", ""),
        idea_keywords = ", ".join(idea.get("target_keywords", [])),
        idea_signals  = signals_text or "  (none)",
        web_backlink  = WEB_BACKLINK,
    )

    payload = {
        "model":       GROQ_MODEL,
        "messages":    [{"role": "user", "content": prompt}],
        "temperature": 0.75,
        "max_tokens":  5000,
        "response_format": {"type": "json_object"},
    }

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
            timeout = 120,
        )
        if r.status_code == 200:
            break
        if r.status_code in (429, 500, 502, 503, 504) and attempt < max_attempts:
            wait = 30 * attempt  # 30s, 60s, 90s
            print(f"   · Groq devolvio {r.status_code}, reintento en {wait}s (intento {attempt}/{max_attempts})...")
            time.sleep(wait)
            continue
        r.raise_for_status()

    content = r.json()["choices"][0]["message"]["content"]

    return json.loads(content)


# ---------------------------------------------------------------------------
# Persistencia
# ---------------------------------------------------------------------------
def save_outputs(script: VideoScript, raw_idea: dict) -> tuple[Path, Path]:
    json_path = OUTPUT_DIR / f"script_{TODAY}.json"
    md_path   = OUTPUT_DIR / f"script_{TODAY}.md"

    # JSON estructurado (consumible por etapa 3)
    json_path.write_text(json.dumps({
        "date":                 script.date,
        "source_idea":          raw_idea,
        "youtube_title":        script.youtube_title,
        "thumbnail_text":       script.thumbnail_text,
        "description":          script.description,
        "tags":                 script.tags,
        "estimated_duration_min": script.estimated_duration_min,
        "word_count":           script.word_count,
        "sections":             [asdict(s) for s in script.sections],
    }, indent=2, ensure_ascii=False), encoding="utf-8")

    # Markdown legible (para curiosear)
    md = [
        f"# {script.youtube_title}",
        "",
        f"_{TODAY} · {script.estimated_duration_min} min · {script.word_count} words_",
        "",
        f"**Thumbnail text:** {script.thumbnail_text}",
        "",
        "---",
        "",
        "## Description",
        "",
        script.description,
        "",
        "**Tags:** " + ", ".join(script.tags),
        "",
        "---",
        "",
        "## Script",
        "",
    ]
    for i, s in enumerate(script.sections, 1):
        label = s.type.upper()
        if s.title:
            label += f" — {s.title}"
        if s.shorts_candidate:
            label += "  🎬 SHORT CANDIDATE"
        md += [
            f"### {i}. {label} ({s.estimated_sec}s)",
            "",
            s.text,
            "",
        ]
        if s.shorts_candidate and s.shorts_alt_hook:
            md += [f"_Short alt hook: {s.shorts_alt_hook}_", ""]
    md_path.write_text("\n".join(md), encoding="utf-8")

    return json_path, md_path


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main() -> None:
    t0 = time.time()
    print(f"\n=== SCRIPT WRITER · {TODAY} ===\n")

    print("1/3  Cargando la idea del dia...")
    idea = load_todays_idea()
    print(f"      -> '{idea.get('title', 'sin titulo')}'")

    print(f"\n2/3  Generando guion con Groq ({GROQ_MODEL})...")
    raw = generate_script(idea)

    # Parseo defensivo
    sections = []
    for s in raw.get("sections", []):
        try:
            sections.append(ScriptSection(
                type             = str(s.get("type", "body")),
                text             = str(s.get("text", "")).strip(),
                title            = str(s.get("title", "")),
                estimated_sec    = int(s.get("estimated_sec", 0)),
                shorts_candidate = bool(s.get("shorts_candidate", False)),
                shorts_alt_hook  = str(s.get("shorts_alt_hook", "")),
            ))
        except (ValueError, TypeError) as e:
            print(f"   ! Seccion malformada, ignorada: {e}")

    if not sections:
        raise RuntimeError("Groq no devolvio secciones validas.")

    script = VideoScript(
        date                  = TODAY,
        source_idea_title     = idea.get("title", ""),
        youtube_title         = str(raw.get("youtube_title", ""))[:80],
        thumbnail_text        = str(raw.get("thumbnail_text", "")),
        description           = str(raw.get("description", "")),
        tags                  = list(raw.get("tags", []))[:25],
        estimated_duration_min = int(raw.get("estimated_duration_min", 10)),
        word_count            = int(raw.get("word_count", 0)),
        sections              = sections,
    )

    print("\n3/3  Guardando guion...")
    json_path, md_path = save_outputs(script, idea)

    n_shorts = sum(1 for s in sections if s.shorts_candidate)
    print(f"\n  Listo en {time.time()-t0:.1f}s")
    print(f"     JSON:       {json_path}")
    print(f"     Resumen MD: {md_path}\n")

    print("=== RESUMEN DEL GUION ===\n")
    print(f"  Titulo:      {script.youtube_title}")
    print(f"  Thumbnail:   {script.thumbnail_text}")
    print(f"  Duracion:    {script.estimated_duration_min} min ({script.word_count} palabras)")
    print(f"  Secciones:   {len(sections)} ({n_shorts} candidatas a Short)")
    print(f"  Tags:        {', '.join(script.tags[:5])}...\n")


if __name__ == "__main__":
    main()
