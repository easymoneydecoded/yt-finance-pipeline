"""
================================================================================
 voiceover.py — Etapa 3 del canal de YouTube automatico
================================================================================

 Que hace:
   1. Lee el JSON de guion generado por la etapa 2 (script_writer.py)
   2. Convierte cada seccion en un MP3 usando edge-tts (gratis, sin clave)
   3. Mide la duracion real de cada audio
   4. Guarda los MP3 en data/audio/ y un manifest JSON con metadatos

 Output:
   data/audio/section_01.mp3, section_02.mp3, ...
   data/audio/audio_manifest_YYYY-MM-DD.json

 Coste: 0 EUR (edge-tts es totalmente gratis)

================================================================================
"""

import os
import json
import time
import asyncio
import wave
import contextlib
from datetime import datetime, timezone
from pathlib import Path

import edge_tts
from mutagen.mp3 import MP3

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
VOICE        = "en-US-AndrewNeural"  # Profesional, neutro, buen ritmo
RATE         = "+0%"                  # Velocidad: 0% = normal; +10% = mas rapida
VOLUME       = "+0%"                  # Volumen: 0% = normal

INPUT_DIR    = Path("data")
OUTPUT_DIR   = Path("data/audio")
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

TODAY        = datetime.now(timezone.utc).strftime("%Y-%m-%d")


# ---------------------------------------------------------------------------
# Carga del guion del dia
# ---------------------------------------------------------------------------
def load_todays_script() -> dict:
    path = INPUT_DIR / f"script_{TODAY}.json"
    if not path.exists():
        candidates = sorted(INPUT_DIR.glob("script_*.json"), reverse=True)
        if not candidates:
            raise FileNotFoundError(
                "No hay ningun script_*.json. Ejecuta script_writer.py primero."
            )
        path = candidates[0]
        print(f"   · Usando script mas reciente: {path.name}")
    return json.loads(path.read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# Generacion de audio con edge-tts
# ---------------------------------------------------------------------------
async def synthesize_section(text: str, output_path: Path) -> None:
    """Genera el MP3 de una seccion."""
    communicate = edge_tts.Communicate(
        text   = text,
        voice  = VOICE,
        rate   = RATE,
        volume = VOLUME,
    )
    await communicate.save(str(output_path))


def get_mp3_duration(path: Path) -> float:
    """Devuelve la duracion real del MP3 en segundos."""
    try:
        return MP3(str(path)).info.length
    except Exception as e:
        print(f"   ! No pude medir duracion de {path.name}: {e}")
        return 0.0


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
async def main_async() -> None:
    t0 = time.time()
    print(f"\n=== VOICEOVER · {TODAY} ===\n")

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

    for i, section in enumerate(sections, start=1):
        section_id   = f"section_{i:02d}"
        section_type = section.get("type", "body")
        text         = section.get("text", "").strip()

        if not text:
            print(f"   ! {section_id} ({section_type}) sin texto, salto.")
            continue

        output_path = OUTPUT_DIR / f"{section_id}.mp3"

        # Retry simple por si edge-tts da fallo de red
        last_error = None
        for attempt in range(1, 4):
            try:
                await synthesize_section(text, output_path)
                last_error = None
                break
            except Exception as e:
                last_error = e
                if attempt < 3:
                    print(f"   · Fallo en {section_id} intento {attempt}: {e}. Reintento en 5s...")
                    await asyncio.sleep(5)

        if last_error:
            print(f"   ! {section_id} fallo definitivamente: {last_error}")
            continue

        duration = get_mp3_duration(output_path)
        total_duration += duration

        manifest_sections.append({
            "section_id":       section_id,
            "section_type":     section_type,
            "section_title":    section.get("title", ""),
            "audio_file":       str(output_path.relative_to(Path("data"))),
            "duration_sec":     round(duration, 2),
            "shorts_candidate": bool(section.get("shorts_candidate", False)),
            "shorts_alt_hook":  section.get("shorts_alt_hook", ""),
            "text":             text,
        })

        print(f"   · {section_id} ({section_type:10s}) -> {duration:5.1f}s")

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
        "sections":              manifest_sections,
    }, indent=2, ensure_ascii=False), encoding="utf-8")

    print(f"\n  Listo en {time.time()-t0:.1f}s")
    print(f"     Audios:    {OUTPUT_DIR}/")
    print(f"     Manifest:  {manifest_path}")

    print(f"\n=== RESUMEN ===")
    print(f"  Secciones sintetizadas: {len(manifest_sections)}/{len(sections)}")
    print(f"  Duracion total:         {total_duration:.1f}s ({total_duration/60:.1f} min)")
    print(f"  Voz:                    {VOICE}")


def main() -> None:
    asyncio.run(main_async())


if __name__ == "__main__":
    main()
