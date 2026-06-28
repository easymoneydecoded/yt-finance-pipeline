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
SCRIPT_WRITER_PROMPT = """You are the head writer of a faceless YouTube channel about PERSONAL FINANCE and INVESTING for an English-speaking, US/UK audience.

Your job: turn the video idea below into a complete, production-ready script for an 8-12 minute long-form video. The script will be:
  - Read by a text-to-speech engine (so write conversational English, not robotic prose)
  - Later sliced into 8-10 short-form vertical clips for Shorts/TikTok/Reels

=== INPUT IDEA ===
Title:    {idea_title}
Hook:     {idea_hook}
Angle:    {idea_angle}
Keywords: {idea_keywords}
Anchored signals:
{idea_signals}
=== END INPUT ===

=== SCRIPT REQUIREMENTS ===

OVERALL TONE:
  - Educational, NOT financial advice. Always frame as "what this means" / "how this works".
  - Conversational and direct. Imagine talking to a smart friend, not lecturing.
  - Use specific numbers, comparisons, and concrete examples — NEVER vague claims.
  - Short sentences. Active voice. Spoken English, not written English.
  - American spelling and idioms (audience is mostly US).

STRUCTURE (in this exact order):
  1. **hook** — 3-5 seconds. Must stop the scroll. Specific number, sharp contradiction, personal stakes question, or insider framing. NO "in this video", NO "today we'll talk about".
  2. **intro** — 15-25 seconds. Quickly establish: what's at stake for the viewer, why now, what they'll get from watching. End with a curiosity gap that promises a payoff later.
  3. **body** — 4 to 6 sections, each 60-120 seconds. Each body section MUST:
       - Have a clear sub-topic and stand on its own (so it can also work as a Short)
       - Start with a mini-hook (the first sentence of the section)
       - Include at least one specific number, comparison, or example
       - Be marked with shorts_candidate: true for the 3 strongest standalone sections
       - For those Shorts-ready sections, provide a shorts_alt_hook: a stronger opener (3-5 sec) if extracted as a standalone Short
  4. **retention** — Insert 1-2 short retention_button sections (15-25 sec) between body sections at strategic points. These tease what's coming next: "But here's where it gets interesting...", "The next part is the one most people get wrong...". Keep them tight.
  5. **outro** — 20-30 seconds. Quick summary of the 2-3 key takeaways, then CTA: subscribe + watch next related video. Include a disclaimer line: "This video is for educational purposes only and is not financial advice."

LENGTH TARGET:
  - Total spoken words: 1500-1800 (for 8-12 minutes at ~150 wpm)
  - Distribute proportionally: hook (~15 words), intro (~50), each body section (~250), each retention (~50), outro (~80)

YOUTUBE METADATA:
  - youtube_title: max 60 chars, CTR-optimized (specific number, curiosity gap, or contradiction). Use sentence case, not Title Case.
  - thumbnail_text: 3-5 words MAX, big and punchy (this is what overlays the thumbnail image)
  - description: 150-250 words. Hook the reader in the first 2 lines (shown above the fold). Include the disclaimer. End with a link to: {web_backlink}
  - tags: 15-20 SEO tags in lowercase English, mix of broad (e.g. "investing") and specific (e.g. "fed rate hike 2026")

=== OUTPUT FORMAT ===
Return ONE JSON object with EXACTLY these top-level fields:
{{
  "youtube_title": "...",
  "thumbnail_text": "...",
  "description": "...",
  "tags": ["...", "..."],
  "estimated_duration_min": 10,
  "word_count": 1650,
  "sections": [
    {{ "type": "hook",       "text": "...", "estimated_sec": 5 }},
    {{ "type": "intro",      "text": "...", "estimated_sec": 20 }},
    {{ "type": "body",       "title": "...", "text": "...", "estimated_sec": 90, "shorts_candidate": true, "shorts_alt_hook": "..." }},
    {{ "type": "retention",  "text": "...", "estimated_sec": 15 }},
    ...
    {{ "type": "outro",      "text": "...", "estimated_sec": 25 }}
  ]
}}

CRITICAL RULES:
  - Output ONLY the JSON object. No prose before or after, no markdown fences.
  - The "text" field of each section is what the TTS will read verbatim. Write it spoken-friendly: contractions, natural pauses (commas, periods), no markdown.
  - For body sections marked shorts_candidate: true, the section MUST work as a standalone 60-90 second clip without needing the rest of the video for context.
  - Word count of all "text" fields combined must be between 1500 and 1800.
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

    r = requests.post(
        GROQ_ENDPOINT,
        headers = {
            "Authorization": f"Bearer {GROQ_API_KEY}",
            "Content-Type":  "application/json",
        },
        json    = payload,
        timeout = 120,
    )
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
