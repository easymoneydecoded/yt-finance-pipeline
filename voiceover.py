"""
================================================================================
 voiceover.py - Etapa 3 del canal de YouTube automatico (v2)
================================================================================

 Que hace:
   1. Lee el JSON de guion generado por la etapa 2
   2. Convierte cada seccion en un MP3 usando edge-tts
   3. VERIFICA que la duracion del audio sea coherente con el texto
   4. Si el audio es sospechosamente corto (bug ocasional de edge-tts), reintenta
   5. Guarda los MP3 y un manifest JSON con metadatos

 Coste: 0 EUR
================================================================================
"""

import os
import json
import time
import re
import asyncio
from datetime import datetime, timezone
from pathlib import Path

import edge_tts
from mutagen.mp3 import MP3

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
VOICE        = "en-US-AndrewNeural"
RATE         = "+0%"
VOLUME       = "+0%"

INPUT_DIR    = Path("data")
OUTPUT_DIR   = Path("data/audio")
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

TODAY        = datetime.now(timezone.utc).strftime("%Y-%m-%d")

# Calidad: si la duracion real es < expected * SANITY_RATIO, reintentar
WORDS_PER_SECOND      = 2.5    # 150 wpm en ingles profesional
SANITY_RATIO          = 0.35   # umbral: audio "mudo" si dura <35% de lo esperado
MAX_TTS_RETRIES       = 3      # intentos de reintentar audios sospechosos


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def count_words(text: str) -> int:
    return len(re.findall(r"\b\w+\b", text or ""))


def expected_duration(text: str) -> float:
    return count_words(text) / WORDS_PER_SECOND


def get_mp3_duration(path: Path) -> float:
    try:
        return MP3(str(path)).info.length
    except Exception:
        return 0.0


def load_todays_script() -> dict:
    path = INPUT_DIR / f"script_{TODAY}.json"
    if not path.exists():
        candidates = sorted(INPUT_DIR.glob("script_*.json"), reverse=True)
        if not candidates:
            raise FileNotFoundError("No hay script_*.json. Ejecuta script_writer.py.")
        path = candidates[0]
        print(f"   . Usando script mas reciente: {path.name}")
    return json.loads(path.read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# Sintesis con verificacion de calidad
# ---------------------------------------------------------------------------
async def synthesize_once(text: str, output_path: Path) -> None:
    communicate = edge_tts.Communicate(
        text   = text,
        voice  = VOICE,
        rate   = RATE,
        volume = VOLUME,
    )
    await communicate.save(str(output_path))


async def synthesize_with_quality_check(text: str, output_path: Path, label: str) -> tuple[float, bool]:
    """
    Sintetiza con reintento si el audio sale 'mudo' (sospechosamente corto).
    Devuelve (duration_sec, is_quality_ok).
    """
    expected = expected_duration(text)
    min_acceptable = expected * SANITY_RATIO

    for attempt in range(1, MAX_TTS_RETRIES + 1):
        try:
            await synthesize_once(text, output_path)
        except Exception as e:
            print(f"     ! intento {attempt}: fallo edge-tts: {e}")
            await asyncio.sleep(3)
            continue

        duration = get_mp3_duration(output_path)

        if duration >= min_acceptable:
            return (duration, True)

        # Audio sospechoso: muy corto
        print(f"     ! intento {attempt}: audio mudo ({duration:.1f}s vs esperado {expected:.1f}s). Reintento...")
        # Borrar el MP3 corrupto antes de reintentar
        try:
            output_path.unlink()
        except FileNotFoundError:
            pass
        await asyncio.sleep(2)

    # Si todos los intentos fallan, dejamos el ultimo audio que haya y avisamos
    final_duration = get_mp3_duration(output_path) if output_path.exists() else 0.0
    return (final_duration, False)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
async def main_async() -> None:
    t0 = time.time()
    print(f"\n=== VOICEOVER . {TODAY} ===\n")

    print("1/3  Cargando guion...")
    script_data = load_todays_script()
    sections = script_data.get("sections", [])
    if not sections:
        raise ValueError("El guion no contiene secciones.")
    print(f"      -> '{script_data.get('youtube_title', '?')}'")
    print(f"      -> {len(sections)} secciones a sintetizar")

    print(f"\n2/3  Sintetizando con edge-tts ({VOICE})...")
    manifest_sections = []
    total_duration = 0.0
    n_quality_warnings = 0

    for i, section in enumerate(sections, start=1):
        section_id   = f"section_{i:02d}"
        section_type = section.get("type", "body")
        text         = section.get("text", "").strip()

        if not text:
            print(f"   ! {section_id} ({section_type}) sin texto, salto.")
            continue

        output_path = OUTPUT_DIR / f"{section_id}.mp3"
        expected = expected_duration(text)

        duration, ok = await synthesize_with_quality_check(text, output_path, section_id)
        total_duration += duration

        quality_flag = "" if ok else "  !! AUDIO SOSPECHOSO"
        if not ok:
            n_quality_warnings += 1

        manifest_sections.append({
            "section_id":       section_id,
            "section_type":     section_type,
            "section_title":    section.get("title", ""),
            "audio_file":       str(output_path.relative_to(Path("data"))),
            "duration_sec":     round(duration, 2),
            "expected_sec":     round(expected, 2),
            "quality_ok":       ok,
            "shorts_candidate": bool(section.get("shorts_candidate", False)),
            "shorts_alt_hook":  section.get("shorts_alt_hook", ""),
            "text":             text,
        })

        print(f"   . {section_id} ({section_type:10s}) -> {duration:5.1f}s (esp. {expected:5.1f}s){quality_flag}")

    if not manifest_sections:
        raise RuntimeError("No se genero ningun audio.")

    print(f"\n3/3  Guardando manifest...")
    manifest_path = OUTPUT_DIR / f"audio_manifest_{TODAY}.json"
    manifest_path.write_text(json.dumps({
        "date":                  TODAY,
        "voice":                 VOICE,
        "rate":                  RATE,
        "youtube_title":         script_data.get("youtube_title", ""),
        "total_duration_sec":    round(total_duration, 2),
        "total_duration_min":    round(total_duration / 60, 2),
        "n_sections":            len(manifest_sections),
        "n_quality_warnings":    n_quality_warnings,
        "sections":              manifest_sections,
    }, indent=2, ensure_ascii=False), encoding="utf-8")

    print(f"\n  Listo en {time.time()-t0:.1f}s")
    print(f"     Audios:    {OUTPUT_DIR}/")
    print(f"     Manifest:  {manifest_path}\n")

    print(f"=== RESUMEN ===")
    print(f"  Secciones sintetizadas: {len(manifest_sections)}/{len(sections)}")
    print(f"  Duracion total:         {total_duration:.1f}s ({total_duration/60:.1f} min)")
    print(f"  Voz:                    {VOICE}")
    if n_quality_warnings:
        print(f"  ! Audios sospechosos:   {n_quality_warnings} (revisa el manifest)")


def main() -> None:
    asyncio.run(main_async())


if __name__ == "__main__":
    main()
