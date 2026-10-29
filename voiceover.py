"""
================================================================================
 voiceover.py - Etapa 3 del canal de YouTube automatico (v3, Kokoro)
================================================================================

 Que hace:
   1. Lee el guion del dia (secciones del video largo + 3 Shorts)
   2. Normaliza el texto para voz ("$4.2B" -> "4.2 billion dollars", "401k"...)
   3. Sintetiza con Kokoro-82M (open source, gratis, corre en CPU) -
      mucho mas natural que edge-tts
   4. Si Kokoro no esta disponible o falla, cae a edge-tts (tambien gratis)
   5. VERIFICA que la duracion sea coherente con el texto (audio "mudo")
   6. Guarda MP3 por seccion y por Short + audio_manifest_YYYY-MM-DD.json

 Voz configurable: KOKORO_VOICE (por defecto af_heart, la mejor valorada).
 Alternativas masculinas: am_michael, am_fenrir, am_puck, bm_george.
 Coste: 0 EUR
================================================================================
"""

import os
import re
import json
import time
import asyncio
import subprocess
from datetime import datetime, timezone
from pathlib import Path

from mutagen.mp3 import MP3

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
KOKORO_VOICE = os.getenv("KOKORO_VOICE", "af_heart")
KOKORO_SPEED = float(os.getenv("KOKORO_SPEED", "1.08"))
KOKORO_RATE  = 24000

EDGE_VOICE   = "en-US-AndrewNeural"

INPUT_DIR    = Path("data")
OUTPUT_DIR   = Path("data/audio")
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

TODAY        = datetime.now(timezone.utc).strftime("%Y-%m-%d")

WORDS_PER_SECOND = 2.6    # ~155 wpm
SANITY_RATIO     = 0.35   # audio "mudo" si dura <35% de lo esperado
MAX_TTS_RETRIES  = 3


# ---------------------------------------------------------------------------
# Normalizacion de texto para TTS
# ---------------------------------------------------------------------------
SCALE = {"K": "thousand", "M": "million", "B": "billion", "T": "trillion"}
REPLACEMENTS = [
    (r"\b401\s*\(?k\)?", "four oh one K"),
    (r"\b403\s*\(?b\)?", "four oh three B"),
    (r"\bS&P\b", "S and P"),
    (r"\bYoY\b", "year over year"),
    (r"\bQoQ\b", "quarter over quarter"),
    (r"\bvs\.?\s", "versus "),
    (r"\be\.g\.", "for example"),
    (r"\bi\.e\.", "that is"),
    (r"\bCEO\b", "C E O"),
    (r"\bETFs\b", "E T Fs"),
    (r"\bETF\b", "E T F"),
    (r"\bIRA\b", "I R A"),
    (r"\bIPO\b", "I P O"),
    (r"\bAI\b", "A I"),
    (r"\bGDP\b", "G D P"),
    (r"\bCPI\b", "C P I"),
    (r"\bEPS\b", "E P S"),
    (r"&", " and "),
]


def normalize_for_tts(text: str) -> str:
    t = text or ""
    # Cuentas de jubilacion antes que las escalas (si no, "401K" se leeria "401 thousand")
    for pat, rep in REPLACEMENTS[:2]:
        t = re.sub(pat, rep, t, flags=re.I)
    # $4.2B / $40B / $3.5 trillion / $65
    t = re.sub(r"\$(\d[\d,]*(?:\.\d+)?)\s*([KMBT])\b",
               lambda m: f"{m.group(1)} {SCALE[m.group(2)]} dollars", t)
    t = re.sub(r"\$(\d[\d,]*(?:\.\d+)?)\s+(thousand|million|billion|trillion)\b",
               r"\1 \2 dollars", t, flags=re.I)
    t = re.sub(r"\$(\d[\d,]*(?:\.\d+)?)", r"\1 dollars", t)
    t = re.sub(r"(\d[\d,]*(?:\.\d+)?)\s*([KMBT])\b(?!\w)",
               lambda m: f"{m.group(1)} {SCALE[m.group(2)]}", t)
    t = re.sub(r"(\d)\s*%", r"\1 percent", t)
    for pat, rep in REPLACEMENTS[2:]:
        t = re.sub(pat, rep, t)
    t = re.sub(r"[()\[\]*_#\"]", "", t)
    return re.sub(r"\s+", " ", t).strip()


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


def wav_to_mp3(wav: Path, mp3: Path) -> None:
    subprocess.run([
        "ffmpeg", "-y", "-loglevel", "error", "-i", str(wav),
        "-ar", "44100", "-ac", "1", "-c:a", "libmp3lame", "-b:a", "192k", str(mp3),
    ], check=True)
    wav.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# Motores TTS
# ---------------------------------------------------------------------------
_kokoro = None
_kokoro_failed = False


def kokoro_pipeline():
    global _kokoro, _kokoro_failed
    if _kokoro is None and not _kokoro_failed:
        try:
            from kokoro import KPipeline
            print(f"   . cargando Kokoro ({KOKORO_VOICE}, speed {KOKORO_SPEED})...")
            _kokoro = KPipeline(lang_code=KOKORO_VOICE[0], repo_id="hexgrad/Kokoro-82M")
        except Exception as e:
            print(f"   ! Kokoro no disponible ({e}); uso edge-tts")
            _kokoro_failed = True
    return _kokoro


def synth_kokoro(text: str, output_path: Path) -> None:
    import numpy as np
    import soundfile as sf

    pipe = kokoro_pipeline()
    if pipe is None:
        raise RuntimeError("Kokoro no disponible")
    chunks = []
    pause = np.zeros(int(KOKORO_RATE * 0.12), dtype=np.float32)
    for _gs, _ps, audio in pipe(text, voice=KOKORO_VOICE, speed=KOKORO_SPEED, split_pattern=r"(?<=[.!?])\s+"):
        if audio is None:
            continue
        audio = audio.detach().cpu().numpy() if hasattr(audio, "detach") else np.asarray(audio)
        chunks += [audio.astype(np.float32), pause]
    if not chunks:
        raise RuntimeError("Kokoro no devolvio audio")
    wav = output_path.with_suffix(".wav")
    sf.write(str(wav), np.concatenate(chunks), KOKORO_RATE)
    wav_to_mp3(wav, output_path)


async def synth_edge(text: str, output_path: Path) -> None:
    import edge_tts
    await edge_tts.Communicate(text=text, voice=EDGE_VOICE).save(str(output_path))


async def synthesize(text: str, output_path: Path) -> tuple[float, bool, str]:
    """Devuelve (duracion, calidad_ok, motor)."""
    spoken = normalize_for_tts(text)
    min_ok = expected_duration(spoken) * SANITY_RATIO
    engines = ([] if _kokoro_failed else ["kokoro"]) + ["edge"]

    for engine in engines:
        for attempt in range(1, MAX_TTS_RETRIES + 1):
            try:
                if engine == "kokoro":
                    synth_kokoro(spoken, output_path)
                else:
                    await synth_edge(spoken, output_path)
            except Exception as e:
                print(f"     ! {engine} intento {attempt}: {e}")
                if engine == "kokoro" and _kokoro_failed:
                    break
                await asyncio.sleep(2)
                continue
            duration = get_mp3_duration(output_path)
            if duration >= min_ok:
                return duration, True, engine
            print(f"     ! {engine} intento {attempt}: audio mudo ({duration:.1f}s). Reintento...")
            output_path.unlink(missing_ok=True)
    final = get_mp3_duration(output_path) if output_path.exists() else 0.0
    return final, False, "none"


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
async def main_async() -> None:
    t0 = time.time()
    print(f"\n=== VOICEOVER . {TODAY} ===\n")

    script_data = load_todays_script()
    sections = script_data.get("sections", [])
    if not sections:
        raise ValueError("El guion no contiene secciones.")
    print(f"1/3  '{script_data.get('youtube_title', '?')}': {len(sections)} secciones, "
          f"{len(script_data.get('shorts', []))} shorts")

    print("\n2/3  Sintetizando video largo...")
    manifest_sections, total, warnings, engines = [], 0.0, 0, set()
    for i, section in enumerate(sections, start=1):
        section_id = f"section_{i:02d}"
        text = section.get("text", "").strip()
        if not text:
            continue
        out = OUTPUT_DIR / f"{section_id}.mp3"
        duration, ok, engine = await synthesize(text, out)
        engines.add(engine)
        total += duration
        warnings += 0 if ok else 1
        manifest_sections.append({
            "section_id":    section_id,
            "section_type":  section.get("type", "body"),
            "section_title": section.get("title", ""),
            "audio_file":    str(out.relative_to(Path("data"))),
            "duration_sec":  round(duration, 2),
            "expected_sec":  round(expected_duration(text), 2),
            "quality_ok":    ok,
            "engine":        engine,
            "text":          text,
        })
        print(f"   . {section_id} ({section.get('type', ''):6s}) -> {duration:5.1f}s [{engine}]"
              + ("" if ok else "  !! AUDIO SOSPECHOSO"))

    if not manifest_sections:
        raise RuntimeError("No se genero ningun audio.")

    print("\n3/3  Sintetizando Shorts...")
    manifest_shorts = []
    for i, short in enumerate(script_data.get("shorts", []), start=1):
        out = OUTPUT_DIR / f"short_{i:02d}.mp3"
        duration, ok, engine = await synthesize(short.get("text", ""), out)
        if not ok or duration < 15:
            print(f"   ! short {i} descartado ({duration:.1f}s)")
            continue
        manifest_shorts.append({
            "short_index": i,
            "title":       short.get("title", ""),
            "audio_file":  str(out.relative_to(Path("data"))),
            "duration_sec": round(duration, 2),
            "engine":      engine,
            "text":        short.get("text", ""),
        })
        print(f"   . short {i} -> {duration:5.1f}s [{engine}]")

    manifest_path = OUTPUT_DIR / f"audio_manifest_{TODAY}.json"
    manifest_path.write_text(json.dumps({
        "date":               TODAY,
        "voice":              KOKORO_VOICE if "kokoro" in engines else EDGE_VOICE,
        "engines":            sorted(engines),
        "youtube_title":      script_data.get("youtube_title", ""),
        "total_duration_sec": round(total, 2),
        "total_duration_min": round(total / 60, 2),
        "n_sections":         len(manifest_sections),
        "n_quality_warnings": warnings,
        "sections":           manifest_sections,
        "shorts":             manifest_shorts,
    }, indent=2, ensure_ascii=False), encoding="utf-8")

    print(f"\n  Listo en {time.time()-t0:.1f}s -> {manifest_path}")
    print(f"  Duracion video largo: {total/60:.1f} min | Shorts: {len(manifest_shorts)} | motor: {', '.join(sorted(engines))}")
    if warnings:
        print(f"  ! Audios sospechosos: {warnings}")


def main() -> None:
    asyncio.run(main_async())


if __name__ == "__main__":
    main()
