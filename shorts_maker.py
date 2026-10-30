"""
================================================================================
 shorts_maker.py - Etapa 6 del canal de YouTube automatico (v3)
================================================================================

 Sustituye a shorts_clipper.py. En vez de recortar el video largo, cada Short
 es un video vertical PROPIO escrito para Shorts:
   1. Guion independiente (gancho en la primera frase + final en bucle)
   2. Su propia voz (etapa 3)
   3. Visuales de datos en 1080x1920 + b-roll recortado a vertical
   4. Captions grandes palabra a palabra (la palabra que suena en amarillo)
   5. short_N_YYYY-MM-DD.mp4 + shorts_manifest_YYYY-MM-DD.json

 Si el guion no trae Shorts, usa el inicio de las secciones "body" como
 respaldo (maximo 58s). Coste: 0 EUR.
================================================================================
"""

import json
import random
import shutil
import time
from datetime import datetime, timezone
from pathlib import Path

import timeline as tl

INPUT_DIR    = Path("data")
AUDIO_DIR    = Path("data/audio")
BROLL_DIR    = Path("data/broll")
MUSIC_DIR    = Path("music")
OUTPUT_DIR   = Path("data/output")
TEMP_DIR     = Path("data/tmp_shorts")
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

TODAY        = datetime.now(timezone.utc).strftime("%Y-%m-%d")
SIZE         = (1080, 1920)
MUSIC_VOLUME = 0.06
MAX_SHORTS   = 3
FALLBACK_MAX = 58.0


def _load(name: str, base: Path) -> dict:
    path = base / f"{name}_{TODAY}.json"
    if not path.exists():
        candidates = sorted(base.glob(f"{name}_*.json"), reverse=True)
        if not candidates:
            raise FileNotFoundError(f"No se encontro {name}_*.json")
        path = candidates[0]
    return json.loads(path.read_text(encoding="utf-8"))


def short_jobs(script: dict, audio: dict) -> list[dict]:
    jobs = []
    script_shorts = script.get("shorts", [])
    for a in audio.get("shorts", []):
        idx = a["short_index"] - 1
        sh = script_shorts[idx] if idx < len(script_shorts) else {}
        jobs.append({"title": a.get("title") or sh.get("title", ""), "audio": INPUT_DIR / a["audio_file"],
                     "visuals": sh.get("visuals", []), "text": a.get("text", ""), "trim": None})
    if jobs:
        return jobs[:MAX_SHORTS]

    print("   ! el guion no trae Shorts propios: uso el inicio de las secciones body")
    sections = script.get("sections", [])
    for i, a in enumerate(audio.get("sections", [])):
        if a.get("section_type") == "body" and i < len(sections):
            jobs.append({"title": sections[i].get("title", ""), "audio": INPUT_DIR / a["audio_file"],
                         "visuals": sections[i].get("visuals", []), "text": a.get("text", ""),
                         "trim": FALLBACK_MAX})
    return jobs[:MAX_SHORTS]


def main() -> None:
    t0 = time.time()
    random.seed()
    print(f"\n=== SHORTS MAKER . {TODAY} ===\n")

    script = _load("script", INPUT_DIR)
    audio = _load("audio_manifest", AUDIO_DIR)
    broll = _load("broll_manifest", BROLL_DIR)
    clips = [INPUT_DIR / c["filename"] for s in broll.get("sections", []) for c in s.get("clips", [])
             if (INPUT_DIR / c["filename"]).exists()]
    if not clips:
        raise RuntimeError("No hay b-roll para los Shorts")

    jobs = short_jobs(script, audio)
    if not jobs:
        print("   . nada que montar, salto la etapa")
        return
    TEMP_DIR.mkdir(parents=True, exist_ok=True)

    manifest = []
    for n, job in enumerate(jobs, 1):
        print(f"\n--- Short {n}/{len(jobs)}: {job['title'][:60]} ---")
        tag = f"_s{n}"
        try:
            voice = TEMP_DIR / f"voice{tag}.wav"
            cmd = ["ffmpeg", "-y", "-loglevel", "error", "-i", str(job["audio"])]
            if job["trim"]:
                cmd += ["-t", f"{job['trim']:.2f}", "-af", f"afade=t=out:st={job['trim']-1.2:.2f}:d=1.2"]
            tl.run(cmd + ["-ar", "44100", "-ac", "1", str(voice)])
            total = tl.ffprobe_duration(voice)

            words = tl.transcribe_words(voice, "" if job["trim"] else job["text"])
            planned = tl.plan_beats([{"start": 0.0, "end": total, "visuals": job["visuals"]}], words, total)
            track, seg_log = tl.build_video_track(planned, lambda t: list(clips), total, SIZE, TEMP_DIR, tag)
            mixed = tl.mix_music(voice, total, MUSIC_DIR, MUSIC_VOLUME, TEMP_DIR / f"mix{tag}.wav",
                                  tl.sfx_events(seg_log))
            subs = tl.build_short_captions(words, TEMP_DIR / f"subs{tag}.ass")
            out = tl.render_final(track, mixed, subs, total, OUTPUT_DIR / f"short_{n}_{TODAY}.mp4",
                                  crf=20, vertical=True)
        except Exception as e:
            print(f"   ! Short {n} fallo: {e}")
            continue

        manifest.append({
            "short_id":     f"short_{n}",
            "filename":     out.name,
            "title":        job["title"],
            "text":         job["text"],
            "duration_sec": round(total, 2),
            "n_visuals":    sum(1 for s in seg_log if s["kind"] == "visual"),
        })
        print(f"   -> {out.name} ({total:.1f}s, {manifest[-1]['n_visuals']} visuales)")

    shutil.rmtree(TEMP_DIR, ignore_errors=True)
    if not manifest:
        raise RuntimeError("No se genero ningun Short")

    path = OUTPUT_DIR / f"shorts_manifest_{TODAY}.json"
    path.write_text(json.dumps({"date": TODAY, "youtube_title": script.get("youtube_title", ""),
                                "n_shorts": len(manifest), "shorts": manifest},
                               indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"\n  Listo en {time.time()-t0:.1f}s: {len(manifest)} Shorts -> {path}")


if __name__ == "__main__":
    main()
