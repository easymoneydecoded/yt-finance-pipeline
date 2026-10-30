"""
================================================================================
 editor.py - Etapa 5 del canal de YouTube automatico (v3, visuales de datos)
================================================================================

 Que hace:
   1. Concatena la voz de todas las secciones
   2. Whisper con tiempos por palabra (sincronia + subtitulos)
   3. Coloca cada visual del guion (grafico real, cifra, comparativa,
      titular) en el momento exacto en que la voz dice su frase ancla
   4. Rellena los huecos con b-roll de la seccion correspondiente
   5. Mezcla voz normalizada (-16 LUFS) + musica + efectos (whoosh/ding)
   6. Subtitulos en frases cortas, marca del canal y render final 1920x1080
   7. Guarda timeline_YYYY-MM-DD.json (inicio de cada seccion -> capitulos)

 Coste: 0 EUR
================================================================================
"""

import json
import random
import shutil
import time
from datetime import datetime, timezone
from pathlib import Path

import timeline as tl

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
INPUT_DIR    = Path("data")
AUDIO_DIR    = Path("data/audio")
BROLL_DIR    = Path("data/broll")
MUSIC_DIR    = Path("music")
OUTPUT_DIR   = Path("data/output")
TEMP_DIR     = Path("data/tmp_editor")
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

TODAY        = datetime.now(timezone.utc).strftime("%Y-%m-%d")
SIZE         = (1920, 1080)
MUSIC_VOLUME = 0.07


def _load(name: str, base: Path) -> dict:
    path = base / f"{name}_{TODAY}.json"
    if not path.exists():
        candidates = sorted(base.glob(f"{name}_*.json"), reverse=True)
        if not candidates:
            raise FileNotFoundError(f"No se encontro {name}_*.json en {base}")
        path = candidates[0]
    print(f"   . {path.name}")
    return json.loads(path.read_text(encoding="utf-8"))


def broll_picker(broll_manifest: dict, section_bounds: list[tuple[float, float]]):
    """Devuelve f(t) -> clips de la seccion que suena en t (o todos si no hay)."""
    per_section, all_clips = [], []
    for sec in broll_manifest.get("sections", []):
        clips = [INPUT_DIR / c["filename"] for c in sec.get("clips", []) if (INPUT_DIR / c["filename"]).exists()]
        per_section.append(clips)
        all_clips += clips

    def pick(t: float) -> list[Path]:
        for i, (s, e) in enumerate(section_bounds):
            if s <= t < e and i < len(per_section) and len(per_section[i]) >= 2:
                return list(per_section[i])
        return list(all_clips)
    return pick


def main() -> None:
    t0 = time.time()
    random.seed()
    print(f"\n=== EDITOR . {TODAY} ===\n")

    print("0/6  Cargando manifests...")
    script = _load("script", INPUT_DIR)
    audio = _load("audio_manifest", AUDIO_DIR)
    broll = _load("broll_manifest", BROLL_DIR)
    if audio.get("total_duration_sec", 0) < 60:
        raise RuntimeError(f"Duracion de voz sospechosa: {audio.get('total_duration_sec')}s")
    TEMP_DIR.mkdir(parents=True, exist_ok=True)

    print("\n1/6  Concatenando voz...")
    voice = tl.concat_audio([INPUT_DIR / s["audio_file"] for s in audio["sections"]], TEMP_DIR / "voice.wav")
    total = tl.ffprobe_duration(voice)
    print(f"      voz: {total:.1f}s ({total/60:.1f} min)")

    # Limites de cada seccion en la linea temporal (escalados a la duracion real)
    raw_durs = [s["duration_sec"] for s in audio["sections"]]
    scale = total / sum(raw_durs) if sum(raw_durs) else 1
    bounds, cursor = [], 0.0
    for d in raw_durs:
        bounds.append((cursor, cursor + d * scale))
        cursor += d * scale

    print("\n2/6  Whisper (tiempos por palabra)...")
    words = tl.transcribe_words(voice, " ".join(s["text"] for s in audio["sections"]))

    print("\n3/6  Planificando visuales de datos...")
    script_sections = script.get("sections", [])
    blocks = []
    for i, (s, e) in enumerate(bounds):
        vis = script_sections[i].get("visuals", []) if i < len(script_sections) else []
        blocks.append({"start": s, "end": e, "visuals": vis})
    planned = tl.plan_beats(blocks, words, total)
    print(f"      {len(planned)} visuales planificados")

    print("\n4/6  Montando pista de video...")
    track, seg_log = tl.build_video_track(planned, broll_picker(broll, bounds), total, SIZE, TEMP_DIR)

    print("\n5/6  Audio + subtitulos...")
    mixed = tl.mix_music(voice, total, MUSIC_DIR, MUSIC_VOLUME, TEMP_DIR / "mix.wav", tl.sfx_events(seg_log))
    subs = tl.build_long_subs(words, TEMP_DIR / "subs.ass")

    print("\n6/6  Render final...")
    final = tl.render_final(track, mixed, subs, total, OUTPUT_DIR / f"video_{TODAY}.mp4")

    (OUTPUT_DIR / f"timeline_{TODAY}.json").write_text(json.dumps({
        "date":     TODAY,
        "duration": round(total, 2),
        "sections": [{"type": script_sections[i].get("type") if i < len(script_sections) else "",
                      "title": script_sections[i].get("title", "") if i < len(script_sections) else "",
                      "start": round(s, 2), "end": round(e, 2)} for i, (s, e) in enumerate(bounds)],
        "segments": seg_log,
    }, indent=2, ensure_ascii=False), encoding="utf-8")

    shutil.rmtree(TEMP_DIR, ignore_errors=True)
    n_vis = sum(1 for s in seg_log if s["kind"] == "visual")
    print(f"\n  Listo en {time.time()-t0:.1f}s -> {final} ({final.stat().st_size/1024/1024:.1f} MB)")
    print(f"  {total/60:.1f} min, {n_vis} visuales de datos, {len(seg_log)-n_vis} cortes de b-roll")


if __name__ == "__main__":
    main()
