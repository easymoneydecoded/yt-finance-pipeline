"""
================================================================================
 script_writer.py - Etapa 2 del canal de YouTube automatico (v3)
================================================================================

 Que hace:
   1. Lee el JSON de ideas generadas por la etapa 1 (morning_pipeline.py)
   2. Coge la primera idea que NO repita nada del historial del canal
   3. Llama a Gemini para escribir el guion SOBRE ESA IDEA (titulo, angulo,
      senales) con un estilo de titulo rotativo
   4. VERIFICA palabras por seccion contra rangos minimos (todas las secciones)
   5. Si alguna seccion esta corta de verdad, REINTENTA con expansion quirurgica
   6. Tolera margenes razonables para no quemar tokens en perfeccionismo
   7. Comprueba que titulo y texto de miniatura no repiten videos anteriores;
      si repiten, regenera solo la metadata
   8. Guarda el guion en JSON (consumible por etapa 3) y en MD (legible)

 Coste: 0 EUR (Groq free tier)

================================================================================
"""

import os
import json
import time
import re
import random
from datetime import datetime, timezone
from pathlib import Path
from dataclasses import dataclass, asdict, field
from typing import Optional

import requests
from dotenv import load_dotenv

import content_history as ch

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
load_dotenv()

GROQ_API_KEY    = os.getenv("GROQ_API_KEY")  # legacy, no usado
GEMINI_API_KEY  = os.getenv("GEMINI_API_KEY")
GEMINI_MODEL    = "gemini-3.5-flash-lite"
GEMINI_ENDPOINT = f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent"

WEB_BACKLINK    = "https://carterapasiva.com"

INPUT_DIR       = Path("data")
OUTPUT_DIR      = Path("data")
OUTPUT_DIR.mkdir(exist_ok=True)

TODAY           = datetime.now(timezone.utc).strftime("%Y-%m-%d")

# Rangos esperados por tipo de seccion (min, max ideal)
SECTION_TARGETS = {
    "hook":      (12,  20),
    "intro":     (50,  70),
    "body":      (220, 280),
    "retention": (35,  50),
    "outro":     (80,  120),
}

# Tolerancia: si la seccion esta al X% del minimo o mas, no reintentamos.
# Solo reintentamos si la seccion esta MUY por debajo (<60% del minimo).
TOLERANCE_RATIO     = 0.60
MAX_RETRY_GENERATION = 2
MAX_METADATA_RETRIES = 3

# Estilos de titulo que rotan para que el canal no suene siempre igual
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
def load_todays_idea(history: list[dict]) -> dict:
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
    print(f"   . Cargadas {len(ideas)} ideas.")
    for n, idea in enumerate(ideas, 1):
        dup = ch.find_duplicate(idea, history, TODAY)
        if dup:
            print(f"   x idea #{n} '{idea.get('title')}' repetida: {dup[1]}")
            continue
        print(f"   . Eligiendo la #{n} (no repite nada del historial).")
        return idea
    raise RuntimeError("Todas las ideas del dia repiten videos ya publicados.")


def pick_title_style(history: list[dict]) -> str:
    recent = set(ch.recent_values(history, "title_style", last_n=3))
    options = [t for t in TITLE_STYLES if t.split(":")[0] not in recent] or TITLE_STYLES
    return random.choice(options)


# ---------------------------------------------------------------------------
# Conteo de palabras y validacion
# ---------------------------------------------------------------------------
def count_words(text: str) -> int:
    return len(re.findall(r"\b\w+\b", text or ""))


def check_section(section_type: str, word_count: int) -> tuple[bool, int, int]:
    """
    Devuelve (is_too_short, min_target, max_target).
    'too_short' significa "tan corto que merece reintento" (<60% del minimo).
    """
    if section_type not in SECTION_TARGETS:
        return (False, 0, 0)
    min_w, max_w = SECTION_TARGETS[section_type]
    threshold = int(min_w * TOLERANCE_RATIO)
    return (word_count < threshold, min_w, max_w)


def measure_script(raw: dict) -> dict:
    """
    Evalua cada seccion. Marca como 'short' solo las que estan MUY por debajo
    del minimo (para no quemar tokens en perfeccionismo).
    """
    sections = raw.get("sections", [])
    per_section = []
    total = 0
    short_sections = []

    for i, s in enumerate(sections):
        stype = s.get("type", "body")
        w = count_words(s.get("text", ""))
        too_short, min_w, max_w = check_section(stype, w)
        per_section.append({
            "i":         i,
            "type":      stype,
            "words":     w,
            "min":       min_w,
            "max":       max_w,
            "too_short": too_short,
        })
        total += w
        if too_short:
            short_sections.append((i, stype, w, min_w))

    return {
        "total_words":    total,
        "per_section":    per_section,
        "short_sections": short_sections,
        "is_too_short":   len(short_sections) > 0 or total < 1400,
    }


# ---------------------------------------------------------------------------
# Prompts
# ---------------------------------------------------------------------------
SCRIPT_WRITER_PROMPT = """You are a world-class YouTube storytelling strategist and financial analyst. Your goal is NOT to report news; your goal is to make the viewer feel like they just unlocked a secret financial superpower. You compete with top-tier creators who master curiosity, urgency, and retention-based editing.

=== YOUR MISSION ===
Write a script that causes viewers to binge-watch and share. Every single sentence must serve one purpose: to keep the viewer from clicking away.

=== TODAY'S TOPIC (THE WHOLE VIDEO MUST BE ABOUT THIS) ===
Title idea: {idea_title}
Hook idea:  {idea_hook}
Angle:      {idea_angle}
Format:     {idea_format}
Keywords:   {idea_keywords}
Anchored on these real signals from today:
{idea_signals}

Stay specific to THIS story: name the companies, people, numbers and dates from
the signals. A generic "hidden trap / your savings are stealing from you" video
that could have been written any other day is a FAILURE.

=== ALREADY PUBLISHED - DO NOT REUSE THESE TITLES OR THUMBNAIL TEXTS ===
{history_block}

=== TONE RULES (CRITICAL) ===
1.  NO "News Anchor" talk. No "In this video we explore..." / "Let's dive in..." / "It's pretty straightforward..."
2.  Be raw, conversational, and opinionated. Use the tone of a high-stakes conversation over a coffee with an expert.
3.  Speed is life. Every paragraph must have a punchy opening.
4.  If a fact isn't shocking, money-related, or directly affecting the viewer's wallet, CUT IT.

=== STRUCTURE - THE "RETENTION ENGINE" (NON-NEGOTIABLE) ===
Do NOT follow a "news report" structure. Follow this "Binge-Watch" structure:

1.  **THE JOLT (Hook - 15-25 words)**: Start with a concrete, shocking paradox or a high-stakes outcome directly related to the viewer's money. DO NOT summarize. Make them feel losing money or missing an opportunity *right now*.
2.  **THE PROMISE (Intro - 30-50 words)**: Connect the Jolt to their specific situation. Explain the "why it matters" immediately. End with a curiosity gap that can only be answered by staying until the end.
3.  **THE STORY (Body - 3-4 sections of 200-250 words each)**:
    - Each body MUST build on the previous one.
    - Use concrete examples: names, actual dollars, dates.
    - Use "Pattern Interrupts": Every 45 seconds, introduce a new angle, a counter-intuitive fact, or a sharp realization.
    - NEVER end a body section with a conclusion. End it with a tease of a bigger problem/solution in the next section.
4.  **THE REVEAL (Outro - 50-80 words)**: Summarize the actionable realization. No boring recaps. Give them ONE concrete step to take. Add the disclaimer briefly. Finish with a sharp CTA.

=== YOUTUBE METADATA (HIGH-CTR) ===
- `youtube_title`: 40-60 chars, about TODAY'S TOPIC, naming its specific subject. MUST use: Curiosity, Contradiction, or Personal Stake.
  Title style for today: {title_style}
  Do NOT start the title with: {overused_openers}
- `thumbnail_text`: 2-4 punchy words specific to today's topic (a name, number or claim from THIS story). Must not match any thumbnail listed above.
- `description`: 150-250 words. Hook, value prop, call to action, a one-line "educational, not financial advice" disclaimer. End with: "Read the Spanish-language deep dive at {web_backlink}"

=== OUTPUT ===
Return ONLY JSON. Do not add any text before or after the JSON.
{{
  "youtube_title": "...",
  "thumbnail_text": "...",
  "description": "...",
  "tags": ["..."],
  "estimated_duration_min": 10,
  "word_count": <REAL count>,
  "sections": [
    {{ "type": "hook",      "text": "..." }},
    {{ "type": "intro",     "text": "..." }},
    {{ "type": "body",      "title": "...", "text": "...", "shorts_candidate": true, "shorts_alt_hook": "..." }},
    {{ "type": "body",      "title": "...", "text": "...", "shorts_candidate": true, "shorts_alt_hook": "..." }},
    {{ "type": "body",      "title": "...", "text": "...", "shorts_candidate": true, "shorts_alt_hook": "..." }},
    {{ "type": "body",      "title": "...", "text": "..." }},
    {{ "type": "outro",     "text": "..." }}
  ]
}}
"""


EXPANSION_PROMPT = """Your previous draft was REJECTED because some sections were too short. Here is your previous draft:

{previous_draft}

Word counts vs targets:
{word_breakdown}

You MUST rewrite ONLY the sections marked SHORT to hit their target word range. Keep ALL OTHER sections (including hook, intro, retention, outro) EXACTLY as they were unless they are also flagged. Do not rewrite what is already fine.

For SHORT body sections, ADD (do not pad with filler):
  - Another specific example with names and dollar figures
  - A historical comparison (e.g. "Back in 2008...")
  - A second-order effect most people miss
  - A common misconception you can correct

For SHORT retention sections, ADD a specific tease of the next section's payoff.

For SHORT outro, ADD a stronger recap or a sharper actionable step.

Return the FULL JSON in the same shape, with corrections applied. Output ONLY the JSON.
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


# ---------------------------------------------------------------------------
# Llamada a Groq con retry para 429/5xx
# ---------------------------------------------------------------------------
def call_groq(prompt: str, max_tokens: int = 16000) -> dict:
    """
    Llama a Gemini (nombre por compat con el resto del pipeline).
    Devuelve dict con el JSON parseado. Lanza RuntimeError si falla del todo.
    """
    if not GEMINI_API_KEY:
        raise RuntimeError("Falta GEMINI_API_KEY")

    payload = {
        "contents": [{
            "parts": [{"text": prompt}]
        }],
        "generationConfig": {
            "temperature":      0.75,
            "maxOutputTokens":  max_tokens,
            "responseMimeType": "application/json",
        },
    }

    content = _call_gemini_with_retries(payload)
    return _parse_gemini_response(content)


def _call_gemini_with_retries(payload: dict, max_attempts: int = 7) -> str:
    """Llama a Gemini con reintentos ante 429/5xx. Devuelve el texto de respuesta."""
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
                timeout = 180,
            )
            if r.status_code == 200:
                data = r.json()
                try:
                    return data["candidates"][0]["content"]["parts"][0]["text"]
                except (KeyError, IndexError) as e:
                    last_err = f"Respuesta Gemini sin texto: {e} / {str(data)[:300]}"
                    print(f"   ! {last_err}")
                    if attempt < max_attempts:
                        wait = 30 * attempt
                        print(f"     reintento en {wait}s...")
                        time.sleep(wait)
                        continue
            elif r.status_code in (429, 500, 502, 503, 504):
                wait = 30 * attempt
                snippet = r.text[:250].replace("\n", " ")
                print(f"   . Gemini devolvio {r.status_code}: {snippet}")
                print(f"     reintento en {wait}s (intento {attempt}/{max_attempts})...")
                time.sleep(wait)
                continue
            else:
                # Otros errores: mostrar body para debug y raise
                print(f"   ! Gemini error {r.status_code}: {r.text[:400]}")
                r.raise_for_status()
        except requests.exceptions.RequestException as e:
            last_err = str(e)
            wait = 30 * attempt
            print(f"   . Gemini error de red: {e}. Reintento en {wait}s...")
            time.sleep(wait)
    raise RuntimeError(f"Gemini fallo tras {max_attempts} intentos: {last_err}")


def _parse_gemini_response(content: str) -> dict:
    """Parsea la respuesta JSON de Gemini de forma tolerante."""
    content = (content or "").strip()

    # Limpiar markdown code blocks si vienen (```json ... ```)
    if content.startswith("```"):
        lines = content.split("\n")
        content = "\n".join(lines[1:])
        if content.rstrip().endswith("```"):
            content = content.rstrip()[:-3]
    content = content.strip()

    if not content:
        raise RuntimeError("Gemini devolvio respuesta vacia")

    # Intento 1: parsear tal cual
    try:
        return json.loads(content)
    except json.JSONDecodeError:
        pass

    # Intento 2: extraer entre primer { y ultimo }
    start = content.find("{")
    end = content.rfind("}")
    if start >= 0 and end > start:
        try:
            return json.loads(content[start:end + 1])
        except json.JSONDecodeError:
            pass

    # Intento 3 (RESCATE): reparar JSON malformado y extraer campo por campo
    print("   . JSON global roto, intentando rescate por campos...")
    rescued = _rescue_script_fields(content)
    if rescued and rescued.get("sections"):
        print(f"   . Rescate OK: {len(rescued.get('sections', []))} secciones recuperadas")
        return rescued

    # Ultimo recurso: mostrar contenido y raise
    print(f"   ! No se encontro JSON valido en la respuesta")
    print(f"     Contenido: '{content[:500]}'")
    raise RuntimeError("Respuesta de Gemini sin JSON valido")


def _rescue_script_fields(content: str) -> dict:
    """
    Ultimo recurso: si el JSON del guion esta malformado, extraer campo por campo
    con regex y balance de llaves. Recupera al menos las secciones del guion.
    """
    import re
    result = {}

    # Extraer campos string simples con regex (youtube_title, thumbnail_text, description)
    for field in ("youtube_title", "thumbnail_text", "description"):
        # Busca "campo": "..." permitiendo escapes internos
        m = re.search(rf'"{field}"\s*:\s*"((?:[^"\\]|\\.)*)"', content)
        if m:
            # Deshacer escapes basicos
            val = m.group(1).replace('\\"', '"').replace('\\n', '\n').replace('\\\\', '\\')
            result[field] = val

    # Extraer tags (array de strings)
    m = re.search(r'"tags"\s*:\s*\[([^\]]*)\]', content, re.DOTALL)
    if m:
        try:
            result["tags"] = json.loads("[" + m.group(1) + "]")
        except json.JSONDecodeError:
            # Fallback: extraer strings individuales
            tags = re.findall(r'"([^"\\]+)"', m.group(1))
            result["tags"] = tags[:20]

    # Extraer sections: recorrer buscando cada objeto {..} dentro de "sections": [..]
    # Localizar el inicio del array sections
    sec_match = re.search(r'"sections"\s*:\s*\[', content)
    if not sec_match:
        return result

    array_start = sec_match.end()
    sections = []
    depth = 0
    obj_start = -1
    in_string = False
    escape = False
    i = array_start

    while i < len(content):
        ch = content[i]
        if escape:
            escape = False
            i += 1
            continue
        if ch == "\\":
            escape = True
            i += 1
            continue
        if ch == '"':
            in_string = not in_string
            i += 1
            continue
        if in_string:
            i += 1
            continue
        if ch == "{":
            if depth == 0:
                obj_start = i
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0 and obj_start != -1:
                obj_str = content[obj_start:i+1]
                try:
                    obj = json.loads(obj_str)
                    if "text" in obj:
                        sections.append(obj)
                except json.JSONDecodeError:
                    # Intentar reparar comas faltantes al final antes de }
                    repaired = re.sub(r',\s*}', '}', obj_str)
                    repaired = re.sub(r',\s*]', ']', repaired)
                    try:
                        obj = json.loads(repaired)
                        if "text" in obj:
                            sections.append(obj)
                    except json.JSONDecodeError:
                        pass
                obj_start = -1
        elif ch == "]" and depth == 0:
            # Fin del array sections
            break
        i += 1

    if sections:
        result["sections"] = sections

    return result


def format_breakdown(stats: dict) -> str:
    lines = []
    for ps in stats["per_section"]:
        if ps["min"] == 0:  # tipo desconocido
            continue
        mark = ""
        if ps["too_short"]:
            mark = f"  <-- SHORT, expand to {ps['min']}-{ps['max']}"
        elif ps["words"] < ps["min"]:
            mark = f"  (slightly under {ps['min']} but acceptable)"
        elif ps["words"] > ps["max"]:
            mark = f"  (over {ps['max']} but acceptable)"
        lines.append(f"  Section {ps['i']+1} ({ps['type']}): {ps['words']} words (target {ps['min']}-{ps['max']}){mark}")
    return "\n".join(lines)


def generate_script(idea: dict, history: list[dict], title_style: str) -> dict:
    if not GEMINI_API_KEY:
        raise RuntimeError("Falta GEMINI_API_KEY")

    signals_text = "\n".join(f"  - {s}" for s in idea.get("source_signals", []))
    overused = ch.overused_title_openers(history)
    prompt = SCRIPT_WRITER_PROMPT.format(
        idea_title       = idea.get("title", ""),
        idea_hook        = idea.get("hook", ""),
        idea_angle       = idea.get("angle", ""),
        idea_format      = idea.get("format", "") or "free",
        idea_keywords    = ", ".join(idea.get("target_keywords", [])),
        idea_signals     = signals_text or "  (none)",
        history_block    = ch.recent_for_prompt(history, limit=40),
        title_style      = title_style,
        overused_openers = ", ".join(f'"{w.capitalize()}"' for w in overused) or "(none)",
        web_backlink     = WEB_BACKLINK,
    )

    print("   . Pasada inicial...")
    raw = call_groq(prompt)
    stats = measure_script(raw)
    print(f"   . Resultado: {stats['total_words']} palabras totales, {len(stats['short_sections'])} secciones muy cortas.")

    for retry in range(1, MAX_RETRY_GENERATION + 1):
        if not stats["is_too_short"]:
            break

        # Listar las secciones cortas para el log
        short_summary = ", ".join(f"{stype}({w}<{m})" for _, stype, w, m in stats["short_sections"])
        if short_summary:
            print(f"   . Secciones cortas: {short_summary}")
        print(f"   . Reintento {retry}/{MAX_RETRY_GENERATION} con expansion quirurgica...")

        expansion_prompt = EXPANSION_PROMPT.format(
            previous_draft = json.dumps(raw, indent=2),
            word_breakdown = format_breakdown(stats),
        )

        raw_new = call_groq(expansion_prompt, max_tokens=8000)
        stats_new = measure_script(raw_new)

        # Solo aceptamos la nueva version si MEJORA
        if stats_new["total_words"] >= stats["total_words"] - 50:  # margen
            raw = raw_new
            stats = stats_new
        else:
            print(f"   ! La expansion empeoro el guion ({stats_new['total_words']} < {stats['total_words']}), descartada.")
        print(f"   . Tras retry {retry}: {stats['total_words']} palabras, {len(stats['short_sections'])} cortas.")

    return raw


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
            new = call_groq(METADATA_RETRY_PROMPT.format(
                idea_title    = idea.get("title", ""),
                idea_angle    = idea.get("angle", ""),
                bad_title     = title,
                bad_thumb     = thumb,
                reason        = dup[1],
                history_block = ch.recent_for_prompt(history, limit=40),
                title_style   = title_style,
            ), max_tokens=500)
        except RuntimeError as e:
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

    raw["youtube_title"]  = title
    raw["thumbnail_text"] = thumb
    return raw


# ---------------------------------------------------------------------------
# Persistencia
# ---------------------------------------------------------------------------
def save_outputs(script: VideoScript, raw_idea: dict, real_word_count: int, title_style: str) -> tuple[Path, Path]:
    json_path = OUTPUT_DIR / f"script_{TODAY}.json"
    md_path   = OUTPUT_DIR / f"script_{TODAY}.md"

    json_path.write_text(json.dumps({
        "date":                 script.date,
        "source_idea":          raw_idea,
        "topic_key":            raw_idea.get("topic_key", ""),
        "entities":             raw_idea.get("entities", []),
        "format":               raw_idea.get("format", ""),
        "title_style":          title_style.split(":")[0],
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

    history = ch.load_history()
    print(f"1/3  Cargando la idea del dia (historial: {len(history)} videos)...")
    idea = load_todays_idea(history)
    print(f"      -> '{idea.get('title', 'sin titulo')}'")
    title_style = pick_title_style(history)
    print(f"      -> estilo de titulo: {title_style.split(':')[0]}")

    print(f"\n2/3  Generando guion con Gemini ({GEMINI_MODEL})...")
    raw = generate_script(idea, history, title_style)
    raw = ensure_unique_metadata(raw, idea, history, title_style)

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
    json_path, md_path = save_outputs(script, idea, real_total, title_style)

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
        min_w, max_w = SECTION_TARGETS.get(s.type, (0, 0))
        in_range = min_w <= w <= max_w if min_w else True
        flag = "" if in_range else f"  (fuera de {min_w}-{max_w})"
        print(f"    {i}. {s.type:10s} {w:>4} palabras{flag}")
    print()


if __name__ == "__main__":
    main()
