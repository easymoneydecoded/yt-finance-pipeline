"""
================================================================================
 script_writer.py — Etapa 2 del canal de YouTube automatico (v2)
================================================================================

 Que hace:
   1. Lee el JSON de ideas generadas por la etapa 1 (morning_pipeline.py)
   2. Coge la primera idea (la mejor, segun el ranking del prompt)
   3. Llama a Groq para escribir un guion completo y estructurado
   4. VERIFICA el word count real (no se fia de lo que dice el modelo)
   5. Si las body sections estan cortas, REINTENTA con expansion forzada
   6. Guarda el guion en JSON (consumible por etapa 3) y en MD (legible)

 Coste: 0 EUR (Groq free tier)

================================================================================
"""

import os
import json
import time
import re
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

WEB_BACKLINK    = "https://carterapasiva.com"

INPUT_DIR       = Path("data")
OUTPUT_DIR      = Path("data")
OUTPUT_DIR.mkdir(exist_ok=True)

TODAY           = datetime.now(timezone.utc).strftime("%Y-%m-%d")

# Word count targets (palabras REALES, contadas tras la respuesta del modelo)
MIN_BODY_WORDS      = 200
MIN_TOTAL_WORDS     = 1500
MAX_RETRY_GENERATION = 2


# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------
@dataclass
class ScriptSection:
    type:             str
    text:             str
    title:            str = ""
    estimated_sec:    int = 0
    shorts_candidate: bool = False
    shorts_alt_hook:  str = ""

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
        candidates = sorted(INPUT_DIR.glob("ideas_*.json"), reverse=True)
        if not candidates:
            raise FileNotFoundError("No hay ideas_*.json. Ejecuta morning_pipeline.py.")
        path = candidates[0]
        print(f"   . Usando JSON mas reciente: {path.name}")

    payload = json.loads(path.read_text(encoding="utf-8"))
    ideas = payload.get("ideas", [])
    if not ideas:
        raise ValueError(f"El archivo {path.name} no contiene ideas.")
    print(f"   . Cargadas {len(ideas)} ideas. Eligiendo la #1.")
    return ideas[0]


# ---------------------------------------------------------------------------
# Conteo de palabras (real, no lo que dice el modelo)
# ---------------------------------------------------------------------------
def count_words(text: str) -> int:
    return len(re.findall(r"\b\w+\b", text or ""))


def measure_script(raw: dict) -> dict:
    sections = raw.get("sections", [])
    per_section = []
    total = 0
    short_bodies = []

    for i, s in enumerate(sections):
        w = count_words(s.get("text", ""))
        per_section.append({"i": i, "type": s.get("type"), "words": w})
        total += w
        if s.get("type") == "body" and w < MIN_BODY_WORDS:
            short_bodies.append((i, w))

    return {
        "total_words":  total,
        "per_section":  per_section,
        "short_bodies": short_bodies,
        "is_too_short": total < MIN_TOTAL_WORDS or len(short_bodies) > 0,
    }


# ---------------------------------------------------------------------------
# Prompts
# ---------------------------------------------------------------------------
SCRIPT_WRITER_PROMPT = """You are the head writer of a popular faceless YouTube channel about personal finance and investing for a US/UK audience. Your scripts compete with channels like Graham Stephan, The Plain Bagel, and Two Cents - conversational, specific, packed with concrete numbers.

=== INPUT IDEA ===
Title:    {idea_title}
Hook:     {idea_hook}
Angle:    {idea_angle}
Keywords: {idea_keywords}
Anchored signals:
{idea_signals}

=== TONE RULES ===
DO:
  - Write like you are texting a smart friend who asked you about money. Direct, specific, occasionally blunt.
  - Use contractions (you are, it is, that is, here is, will not, cannot).
  - Start sentences with conjunctions (And, But, So) when natural.
  - Use concrete numbers EVERYWHERE. Specific examples with names/cities/figures.
  - Acknowledge counterarguments briefly.

DON'T (AI-voice tells that kill retention):
  - "In this video we will explore" / "Let us dive in" / "Without further ado"
  - "It is pretty straightforward" / "It all starts with" / "So, what is behind this?"
  - Rhetorical questions inside body sections - save those for hooks.
  - Vague hedges: "could", "might", "potentially". Be direct.

=== STRUCTURE ===

1. **hook** (12-20 words) - Stop the scroll. Specific stakes, sharp contradiction, insider framing, or cost-of-inaction. NEVER repeat the input hook verbatim - ELEVATE it.

2. **intro** (50-70 words, ~20 sec) - What is happening + why now + what specifically the viewer will learn. End with curiosity gap.

3. **body** - Exactly 4 sections. THIS IS NON-NEGOTIABLE: each body section MUST contain BETWEEN 220 AND 280 WORDS of text. Not 100, not 150. 220-280. Count them. If you write less, you have FAILED the task and the output will be rejected.
   - Open with a mini-hook (specific claim or number, NOT a question)
   - At least TWO specific numbers OR a worked example with real figures
   - End with a transition into the next section
   - Mark 3 of the 4 as shorts_candidate: true
   - For Shorts candidates, write a shorts_alt_hook (8-15 words)

4. **retention** - 2 sections of 35-50 words each. Tease a SPECIFIC payoff.

5. **outro** (80-120 words) - Recap key takeaway + actionable next step + disclaimer "This is for education, not financial advice - talk to a fiduciary for your situation" + CTA "If this changed how you think about [topic], hit subscribe - I break down one of these every week."

=== HARD WORD COUNT REQUIREMENT (READ TWICE) ===
The TOTAL word count of all text fields combined must be 1500-1800 words.
This means:
  - hook:        12-20 words
  - intro:       50-70 words
  - body x 4:    220-280 each (total 880-1120)
  - retention x 2: 35-50 each (total 70-100)
  - outro:       80-120 words
  - GRAND TOTAL: 1500-1800

Before you finish writing, MENTALLY COUNT the words in each body section. If any body is under 220, EXPAND IT with:
  - Another specific example with names and dollar figures
  - A historical comparison ("Back in 2008, the same thing happened to...")
  - A second-order effect ("And here is what most people miss: this also affects...")
  - A common misconception you can correct
DO NOT pad with filler like "and so on" or repetition.

=== STRUCTURAL DISCIPLINE ===
Body sections must each cover a DIFFERENT angle. The last body must introduce a FRESH angle (actionable response, contrarian view, historical parallel, surprising second-order effect). Body sections NEVER include summaries, CTAs, or disclaimers - those go ONLY in the outro.

=== YOUTUBE METADATA ===
  - youtube_title: max 60 chars. Sentence case. CTR-optimized.
  - thumbnail_text: 2-4 BIG punchy words (all caps OK).
  - description: 150-250 words. First 2 lines hook the reader. Include the disclaimer. End with: "Read the Spanish-language deep dive at {web_backlink}"
  - tags: 15-20 lowercase SEO tags, mix broad + specific.

=== OUTPUT ===
Return ONE JSON object with these EXACT fields:
{{
  "youtube_title": "...",
  "thumbnail_text": "...",
  "description": "...",
  "tags": ["..."],
  "estimated_duration_min": 10,
  "word_count": <REAL count of all section texts combined>,
  "sections": [
    {{ "type": "hook",      "text": "...", "estimated_sec": 5 }},
    {{ "type": "intro",     "text": "...", "estimated_sec": 22 }},
    {{ "type": "body",      "title": "...", "text": "...", "estimated_sec": 100, "shorts_candidate": true, "shorts_alt_hook": "..." }},
    {{ "type": "body",      "title": "...", "text": "...", "estimated_sec": 100, "shorts_candidate": true, "shorts_alt_hook": "..." }},
    {{ "type": "retention", "text": "...", "estimated_sec": 18 }},
    {{ "type": "body",      "title": "...", "text": "...", "estimated_sec": 100, "shorts_candidate": true, "shorts_alt_hook": "..." }},
    {{ "type": "retention", "text": "...", "estimated_sec": 18 }},
    {{ "type": "body",      "title": "...", "text": "...", "estimated_sec": 100, "shorts_candidate": false }},
    {{ "type": "outro",     "text": "...", "estimated_sec": 35 }}
  ]
}}

Output ONLY the JSON. No prose before or after. No markdown fences.
"""


EXPANSION_PROMPT = """Your previous draft was REJECTED because the body sections were too short. Here is the draft you wrote:

{previous_draft}

Word counts of each section (from your draft):
{word_breakdown}

You MUST rewrite ALL body sections marked SHORT to contain 220-280 words EACH.

For each short body section, ADD:
  - Another specific example with names and dollar figures
  - OR a historical comparison
  - OR a second-order effect most people miss
  - OR a common misconception you can correct

DO NOT change the hook, intro, retention sections, or outro unless they are also flagged as short. Only fix what is short. Keep everything else identical.

Return the SAME JSON shape as before, with the expanded sections. Output ONLY the JSON.
"""


# ---------------------------------------------------------------------------
# Llamada a Groq con retry para 429/5xx
# ---------------------------------------------------------------------------
def call_groq(prompt: str, max_tokens: int = 5500) -> dict:
    payload = {
        "model":       GROQ_MODEL,
        "messages":    [{"role": "user", "content": prompt}],
        "temperature": 0.75,
        "max_tokens":  max_tokens,
        "response_format": {"type": "json_object"},
    }

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
            wait = 30 * attempt
            print(f"   . Groq devolvio {r.status_code}, reintento en {wait}s (intento {attempt}/{max_attempts})...")
            time.sleep(wait)
            continue
        r.raise_for_status()

    content = r.json()["choices"][0]["message"]["content"]
    return json.loads(content)


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

    print("   . Pasada inicial...")
    raw = call_groq(prompt)
    stats = measure_script(raw)
    print(f"   . Resultado: {stats['total_words']} palabras, {len(stats['short_bodies'])} body(s) cortas.")

    for retry in range(1, MAX_RETRY_GENERATION + 1):
        if not stats["is_too_short"]:
            break

        print(f"   . Guion corto. Reintento {retry}/{MAX_RETRY_GENERATION} con expansion forzada...")

        breakdown_lines = []
        for ps in stats["per_section"]:
            mark = ""
            if ps["type"] == "body" and ps["words"] < MIN_BODY_WORDS:
                mark = "  <-- SHORT, EXPAND TO 220-280"
            breakdown_lines.append(f"  Section {ps['i']+1} ({ps['type']}): {ps['words']} words{mark}")
        word_breakdown = "\n".join(breakdown_lines)

        expansion_prompt = EXPANSION_PROMPT.format(
            previous_draft = json.dumps(raw, indent=2),
            word_breakdown = word_breakdown,
        )

        raw = call_groq(expansion_prompt, max_tokens=6500)
        stats = measure_script(raw)
        print(f"   . Tras expansion: {stats['total_words']} palabras, {len(stats['short_bodies'])} body(s) cortas.")

    return raw


# ---------------------------------------------------------------------------
# Persistencia
# ---------------------------------------------------------------------------
def save_outputs(script: VideoScript, raw_idea: dict, real_word_count: int) -> tuple[Path, Path]:
    json_path = OUTPUT_DIR / f"script_{TODAY}.json"
    md_path   = OUTPUT_DIR / f"script_{TODAY}.md"

    json_path.write_text(json.dumps({
        "date":                 script.date,
        "source_idea":          raw_idea,
        "youtube_title":        script.youtube_title,
        "thumbnail_text":       script.thumbnail_text,
        "description":          script.description,
        "tags":                 script.tags,
        "estimated_duration_min": script.estimated_duration_min,
        "word_count_real":      real_word_count,
        "word_count_claimed":   script.word_count,
        "sections":             [asdict(s) for s in script.sections],
    }, indent=2, ensure_ascii=False), encoding="utf-8")

    md = [
        f"# {script.youtube_title}",
        "",
        f"_{TODAY} . {script.estimated_duration_min} min . {real_word_count} words (real)_",
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
            label += f" - {s.title}"
        if s.shorts_candidate:
            label += "  [SHORT CANDIDATE]"
        w = count_words(s.text)
        md += [
            f"### {i}. {label} ({s.estimated_sec}s . {w} words)",
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
    print(f"\n=== SCRIPT WRITER . {TODAY} ===\n")

    print("1/3  Cargando la idea del dia...")
    idea = load_todays_idea()
    print(f"      -> '{idea.get('title', 'sin titulo')}'")

    print(f"\n2/3  Generando guion con Groq ({GROQ_MODEL})...")
    raw = generate_script(idea)

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

    real_total = sum(count_words(s.text) for s in sections)

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
    json_path, md_path = save_outputs(script, idea, real_total)

    n_shorts = sum(1 for s in sections if s.shorts_candidate)
    estimated_minutes = real_total / 150

    print(f"\n  Listo en {time.time()-t0:.1f}s")
    print(f"     JSON:       {json_path}")
    print(f"     Resumen MD: {md_path}\n")

    print("=== RESUMEN DEL GUION ===\n")
    print(f"  Titulo:           {script.youtube_title}")
    print(f"  Thumbnail:        {script.thumbnail_text}")
    print(f"  Palabras reales:  {real_total} (modelo declaro {script.word_count})")
    print(f"  Duracion proy:    ~{estimated_minutes:.1f} min hablado")
    print(f"  Secciones:        {len(sections)} ({n_shorts} candidatas a Short)")

    print(f"\n  Desglose:")
    for i, s in enumerate(sections, 1):
        w = count_words(s.text)
        flag = ""
        if s.type == "body" and w < MIN_BODY_WORDS:
            flag = "  ! corto"
        print(f"    {i}. {s.type:10s} {w:>4} palabras{flag}")
    print()


if __name__ == "__main__":
    main()
