"""
================================================================================
 timeline.py - Montaje compartido por el video largo (editor.py) y los Shorts
================================================================================

 - Transcribe la voz con Whisper con tiempos POR PALABRA
 - Coloca cada visual de datos en el instante en que la voz dice su "anchor"
 - Rellena los huecos con b-roll (cortes de 2.5-4s con zoom suave)
 - Genera subtitulos: frases cortas (largo) o palabra a palabra (Shorts)
 - Mezcla musica de fondo
================================================================================
"""

import os
import random
import re
import subprocess
from pathlib import Path

import visuals

FPS           = 30
WHISPER_MODEL = "base.en"
BROLL_MIN     = 2.5
BROLL_MAX     = 4.0
MIN_GAP_FILL  = 0.6   # huecos menores se absorben alargando el visual anterior


# ---------------------------------------------------------------------------
# Utilidades
# ---------------------------------------------------------------------------
def run(cmd: list[str], desc: str = "") -> None:
    if desc:
        print(f"   . {desc}")
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        print(f"   ! FALLO: {' '.join(cmd[:6])}...")
        print(f"   ! stderr: {result.stderr[-600:]}")
        raise RuntimeError(f"FFmpeg fallo: {desc}")


def ffprobe_duration(path: Path) -> float:
    result = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "default=noprint_wrappers=1:nokey=1", str(path)],
        capture_output=True, text=True,
    )
    try:
        return float(result.stdout.strip())
    except (ValueError, TypeError):
        return 0.0


def concat_audio(files: list[Path], out: Path) -> Path:
    list_file = out.with_suffix(".txt")
    list_file.write_text("".join(f"file '{f.resolve().as_posix()}'\n" for f in files), encoding="utf-8")
    run(["ffmpeg", "-y", "-loglevel", "error", "-f", "concat", "-safe", "0", "-i", str(list_file),
         "-c:a", "pcm_s16le", "-ar", "44100", "-ac", "1", str(out)], "concatenando voz")
    return out


# ---------------------------------------------------------------------------
# Whisper con tiempos por palabra
# ---------------------------------------------------------------------------
_whisper = None


def align_to_script(words: list[dict], script_text: str) -> list[dict]:
    """
    Whisper acierta los tiempos pero no siempre la ortografia ("CostCo").
    Alinea sus palabras con el guion real y usa el texto del guion.
    """
    from difflib import SequenceMatcher
    script_tokens = script_text.split()
    a = [_norm(w["word"]) for w in words]
    b = [_norm(t) for t in script_tokens]
    fixed = 0
    for tag, i1, i2, j1, j2 in SequenceMatcher(None, a, b, autojunk=False).get_opcodes():
        if tag == "equal" or (tag == "replace" and i2 - i1 == j2 - j1):
            for k in range(i2 - i1):
                if words[i1 + k]["word"] != script_tokens[j1 + k]:
                    words[i1 + k]["word"] = script_tokens[j1 + k]
                    fixed += 1
    if fixed:
        print(f"   . {fixed} palabras corregidas con el texto del guion")
    return words


def transcribe_words(audio: Path, script_text: str = "") -> list[dict]:
    global _whisper
    import whisper
    if _whisper is None:
        print(f"   . cargando whisper '{WHISPER_MODEL}'...")
        _whisper = whisper.load_model(WHISPER_MODEL)
    result = _whisper.transcribe(str(audio), language="en", fp16=False, verbose=None,
                                 word_timestamps=True, condition_on_previous_text=False)
    words = []
    for seg in result.get("segments", []):
        for w in seg.get("words", []):
            token = w["word"].strip()
            if token:
                words.append({"word": token, "start": float(w["start"]), "end": float(w["end"])})
    print(f"   . {len(words)} palabras transcritas")
    return align_to_script(words, script_text) if script_text else words


def _norm(token: str) -> str:
    return re.sub(r"[^a-z0-9]", "", token.lower())


def find_anchor(words: list[dict], anchor: str, t0: float, t1: float) -> float | None:
    """Instante en que se dice 'anchor' dentro de [t0, t1]. None si no aparece."""
    target = [_norm(t) for t in (anchor or "").split() if _norm(t)]
    if not target:
        return None
    window = [w for w in words if t0 - 0.5 <= w["start"] <= t1]
    best, best_score = None, 0.0
    for j in range(len(window)):
        seq = [_norm(w["word"]) for w in window[j:j + len(target)]]
        score = sum(1 for a, b in zip(target, seq) if a == b) / len(target)
        if score > best_score:
            best, best_score = window[j]["start"], score
    return best if best_score >= 0.5 else None


# ---------------------------------------------------------------------------
# Planificacion: cuando sale cada visual
# ---------------------------------------------------------------------------
def plan_beats(blocks: list[dict], words: list[dict], total: float, force_first_at_zero: bool = True) -> list[dict]:
    """
    blocks: [{"start": s, "end": e, "visuals": [...]}] (secciones o un Short entero)
    Devuelve [{"beat": {...}, "start": t, "dur": d}] ordenado y sin solapes.
    """
    planned = []
    for b in blocks:
        vis = [v for v in b.get("visuals", []) if isinstance(v, dict)]
        span = max(0.1, b["end"] - b["start"])
        for k, v in enumerate(vis):
            t = find_anchor(words, v.get("anchor", ""), b["start"], b["end"])
            if t is None:
                t = b["start"] + span * (k + 0.5) / (len(vis) + 0.5)
            planned.append({"beat": v, "start": max(0.0, t - 0.15),
                            "dur": visuals.DEFAULT_DURATION.get(v.get("type"), 4.5)})
    planned.sort(key=lambda p: p["start"])
    if planned and force_first_at_zero and planned[0]["start"] < 4.0:
        planned[0]["start"] = 0.0

    out, cursor = [], 0.0
    for p in planned:
        start = max(p["start"], cursor)
        if start >= total - 1.5:
            continue
        p["start"], p["dur"] = start, min(p["dur"], total - start)
        out.append(p)
        cursor = start + p["dur"]
    return out


# ---------------------------------------------------------------------------
# Pista de video
# ---------------------------------------------------------------------------
def _broll_filter(w: int, h: int) -> str:
    zoom_in = random.random() < 0.5
    z = "min(zoom+0.0012,1.12)" if zoom_in else "if(eq(on,0),1.12,max(zoom-0.0012,1.0))"
    return (f"scale={w}:{h}:force_original_aspect_ratio=increase,crop={w}:{h},fps={FPS},"
            f"zoompan=z='{z}':d=1:x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':s={w}x{h},"
            f"eq=contrast=1.06:saturation=1.08")


def build_video_track(planned: list[dict], broll_for_time, total: float, size: tuple[int, int],
                      temp_dir: Path, tag: str = "") -> tuple[Path, list[dict]]:
    """
    broll_for_time(t) -> lista de clips candidatos (Path) para el instante t.
    Devuelve (pista_mp4, log de segmentos).
    """
    w, h = size
    temp_dir.mkdir(parents=True, exist_ok=True)
    segments, log = [], []

    # 1) Renderizar visuales; los que fallen se convierten en hueco de b-roll
    rendered = []
    for i, p in enumerate(planned):
        out = temp_dir / f"vis{tag}_{i:03d}.mp4"
        print(f"   . visual {i+1}/{len(planned)} [{p['beat'].get('type')}] @ {p['start']:.1f}s ({p['dur']:.1f}s)")
        if visuals.render_visual(p["beat"], size, p["dur"], out):
            rendered.append({**p, "file": out})

    # 2) Recorrer la linea temporal rellenando huecos con b-roll
    cursor, last_clip, idx = 0.0, None, 0
    usage: dict = {}
    events = rendered + [{"start": total, "dur": 0, "file": None}]
    for ev in events:
        gap = ev["start"] - cursor
        while gap > 0.05:
            if gap < MIN_GAP_FILL and segments:
                segments[-1]["dur"] += gap
                cursor += gap
                break
            dur = gap if gap <= BROLL_MAX + 1.0 else random.uniform(BROLL_MIN, BROLL_MAX)
            pool = [c for c in broll_for_time(cursor) if c != last_clip] or broll_for_time(cursor)
            if not pool:
                raise RuntimeError("No hay clips de b-roll disponibles")
            pool.sort(key=lambda c: usage.get(c, 0) + random.random())
            clip = pool[0]
            usage[clip] = usage.get(clip, 0) + 1
            last_clip = clip
            segments.append({"kind": "broll", "clip": clip, "dur": dur})
            cursor += dur
            gap -= dur
        if ev["file"] is not None:
            segments.append({"kind": "visual", "file": ev["file"], "dur": ev["dur"],
                             "beat": ev["beat"].get("type")})
            cursor = ev["start"] + ev["dur"]

    # 3) Normalizar cada segmento al mismo formato
    files = []
    for i, seg in enumerate(segments):
        out = temp_dir / f"seg{tag}_{i:03d}.mp4"
        if seg["kind"] == "broll":
            clip_len = ffprobe_duration(seg["clip"])
            start = random.uniform(0, max(0.0, clip_len - seg["dur"] - 0.2)) if clip_len > seg["dur"] else 0
            run(["ffmpeg", "-y", "-loglevel", "error", "-ss", f"{start:.3f}", "-stream_loop", "-1",
                 "-i", str(seg["clip"]), "-t", f"{seg['dur']:.3f}", "-vf", _broll_filter(w, h), "-an",
                 "-c:v", "libx264", "-preset", "veryfast", "-crf", "21", "-pix_fmt", "yuv420p", "-r", str(FPS),
                 str(out)])
        else:
            run(["ffmpeg", "-y", "-loglevel", "error", "-i", str(seg["file"]), "-t", f"{seg['dur']:.3f}",
                 "-vf", f"tpad=stop_mode=clone:stop_duration=2,fps={FPS}", "-an",
                 "-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-pix_fmt", "yuv420p", "-r", str(FPS),
                 str(out)])
        files.append(out)
        log.append({k: (str(v) if isinstance(v, Path) else v) for k, v in seg.items()})

    n_vis = sum(1 for s in segments if s["kind"] == "visual")
    print(f"   . {len(segments)} segmentos ({n_vis} visuales de datos, {len(segments)-n_vis} b-roll)")

    list_file = temp_dir / f"track{tag}.txt"
    list_file.write_text("".join(f"file '{f.resolve().as_posix()}'\n" for f in files), encoding="utf-8")
    track = temp_dir / f"track{tag}.mp4"
    run(["ffmpeg", "-y", "-loglevel", "error", "-f", "concat", "-safe", "0", "-i", str(list_file),
         "-c", "copy", str(track)], "concatenando pista de video")
    return track, log


# ---------------------------------------------------------------------------
# Audio: voz + musica
# ---------------------------------------------------------------------------
SFX_DIR    = Path("data/sfx")
SFX_VOLUME = 0.32
# Efectos generados con ffmpeg (sin archivos externos ni derechos de autor)
SFX = {
    "whoosh": "anoisesrc=d=0.55:c=pink:a=0.55,highpass=f=350,lowpass=f=4500,"
              "afade=t=in:st=0:d=0.25,afade=t=out:st=0.25:d=0.3",
    "ding":   "sine=f=1318:d=0.4,afade=t=out:st=0.01:d=0.39,volume=0.55",
}


def sfx_file(kind: str) -> Path:
    SFX_DIR.mkdir(parents=True, exist_ok=True)
    path = SFX_DIR / f"{kind}.wav"
    if not path.exists():
        run(["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i", SFX[kind],
             "-ar", "44100", "-ac", "1", str(path)])
    return path


def sfx_events(seg_log: list[dict]) -> list[tuple[float, str]]:
    """Whoosh al entrar cada visual de datos y ding cuando aparece una cifra."""
    events, t = [], 0.0
    for seg in seg_log:
        if seg["kind"] == "visual":
            events.append((max(0.0, t - 0.12), "whoosh"))
            if seg.get("beat") == "stat":
                events.append((t + 0.35, "ding"))
        t += seg["dur"]
    return events


def mix_music(voice: Path, duration: float, music_dir: Path, volume: float, out: Path,
              events: list[tuple[float, str]] | None = None) -> Path:
    """Voz normalizada (-16 LUFS) + musica de fondo + efectos de sonido."""
    tracks = sorted(music_dir.glob("*.mp3")) + sorted(music_dir.glob("*.wav"))
    inputs = ["-i", str(voice)]
    # Silencio al final ANTES de loudnorm: su ventana de ~3 s no se vaciaba al acabar el audio (ffmpeg de
    # Ubuntu) y los Shorts perdian la ultima frase (audio 51,0 s en un video de 53,9 s). El -t recorta luego.
    filters = ["[0:a]apad=pad_dur=4,loudnorm=I=-16:TP=-1.5:LRA=11[v]"]
    labels = ["[v]"]
    if tracks:
        chosen = random.choice(tracks)
        print(f"   . musica: {chosen.name}")
        inputs += ["-stream_loop", "-1", "-i", str(chosen)]
        filters.append(f"[1:a]volume={volume},afade=t=in:d=1.5,"
                       f"afade=t=out:st={max(0, duration-2.5):.2f}:d=2.5[m]")
        labels.append("[m]")
    else:
        print("   ! sin musica de fondo")
    for k, (t, kind) in enumerate(events or []):
        idx = len(labels)
        inputs += ["-i", str(sfx_file(kind))]
        ms = int(t * 1000)
        filters.append(f"[{idx}:a]adelay={ms}|{ms},volume={SFX_VOLUME}[x{k}]")
        labels.append(f"[x{k}]")
    filters.append(f"{''.join(labels)}amix=inputs={len(labels)}:duration=first:dropout_transition=0:normalize=0[out]")
    run(["ffmpeg", "-y", "-loglevel", "error", *inputs, "-filter_complex", ";".join(filters),
         "-map", "[out]", "-c:a", "pcm_s16le", "-ar", "44100", "-t", f"{duration:.3f}", str(out)],
        f"mezclando voz + musica + {len(events or [])} efectos")
    mixed = ffprobe_duration(out)
    if mixed < duration - 0.2:
        print(f"   ! la mezcla dura {mixed:.1f}s y la voz {duration:.1f}s: el final saldra sin audio")
    return out


# ---------------------------------------------------------------------------
# Subtitulos
# ---------------------------------------------------------------------------
def ass_time(seconds: float) -> str:
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    return f"{h}:{m:02d}:{seconds % 60:05.2f}"


def chunk_words(words: list[dict], max_chars: int, max_words: int, max_sec: float) -> list[dict]:
    chunks, cur = [], []
    for w in words:
        text = " ".join(x["word"] for x in cur + [w])
        too_long = cur and (len(text) > max_chars or len(cur) >= max_words or w["end"] - cur[0]["start"] > max_sec)
        pause = cur and w["start"] - cur[-1]["end"] > 0.45
        if too_long or pause:
            chunks.append(cur)
            cur = []
        cur.append(w)
        if w["word"].endswith((".", "?", "!")):
            chunks.append(cur)
            cur = []
    if cur:
        chunks.append(cur)
    return [{"start": c[0]["start"], "end": c[-1]["end"], "text": " ".join(x["word"] for x in c)} for c in chunks]


ASS_LONG = """[Script Info]
ScriptType: v4.00+
PlayResX: 1920
PlayResY: 1080
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Default,DejaVu Sans,58,&H00FFFFFF,&H00FFFFFF,&H00000000,&H64000000,1,0,0,0,100,100,0,0,1,4,1,2,120,120,48,1
Style: Subscribe,Anton,62,&H00FFFFFF,&H00FFFFFF,&H000000E6,&H64000000,0,0,0,0,100,100,2,0,3,14,0,3,90,90,190,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""

ASS_SHORT = """[Script Info]
ScriptType: v4.00+
PlayResX: 1080
PlayResY: 1920
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Default,Anton,104,&H00FFFFFF,&H00FFFFFF,&H00000000,&H96000000,0,0,0,0,100,100,1,0,1,7,3,2,60,60,400,1
Style: Hook,Anton,138,&H0000D7FF,&H0000D7FF,&H40000000,&HB4000000,0,0,0,0,100,100,1,0,3,18,0,8,70,70,330,1
Style: Cta,Anton,82,&H00FFFFFF,&H00FFFFFF,&H40000000,&HB4000000,0,0,0,0,100,100,1,0,3,16,0,8,70,70,360,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""


def _clean(text: str) -> str:
    return text.replace("{", "").replace("}", "").replace("\n", " ").strip()


SUBSCRIBE_EARLY = 90.0   # el primer recordatorio, cuando el espectador ya esta enganchado
SUBSCRIBE_END = 8.0      # el ultimo, mientras el outro pide la suscripcion


def build_long_subs(words: list[dict], out: Path, total: float = 0.0) -> Path:
    """Subtitulos del video largo; con 'total', un boton rojo de SUBSCRIBE abajo a la derecha hacia el
    minuto 1:30 y en los ultimos segundos."""
    lines = [ASS_LONG]
    for c in chunk_words(words, max_chars=44, max_words=9, max_sec=3.2):
        lines.append(f"Dialogue: 0,{ass_time(c['start'])},{ass_time(c['end'] + 0.1)},Default,,0,0,0,,{_clean(c['text'])}")
    pop = r"{\fscx60\fscy60\t(0,200,\fscx100\fscy100)}"
    for a, b in ((SUBSCRIBE_EARLY, SUBSCRIBE_EARLY + 5), (total - SUBSCRIBE_END, total)):
        if total > 3 * 60 and a > 0:
            lines.append(f"Dialogue: 1,{ass_time(a)},{ass_time(b)},Subscribe,,0,0,0,,{pop}SUBSCRIBE")
    out.write_text("\n".join(lines), encoding="utf-8")
    return out


HOOK_SECONDS = 2.5   # TikTok: la mayoria se iba en el segundo 1 (media de 2,5 s vistos de 52)
CTA_SECONDS = 2.5


def build_short_captions(words: list[dict], out: Path, hook: str = "", total: float = 0.0,
                         cta: str = "FOLLOW FOR DAILY\\NMARKET NEWS") -> Path:
    """Captions grandes de 1-3 palabras; la palabra que suena va en amarillo. Con 'hook', el titular
    del Short sale enorme arriba los primeros segundos (el primer plano suele ser b-roll que no dice
    nada); con 'total', una llamada a seguir al final (solo texto: la voz cierra en bucle)."""
    lines = [ASS_SHORT]
    if hook:
        pop = r"{\fscx70\fscy70\t(0,160,\fscx100\fscy100)}"
        lines.append(f"Dialogue: 1,{ass_time(0)},{ass_time(HOOK_SECONDS)},Hook,,0,0,0,,{pop}{_clean(hook).upper()}")
    if total > HOOK_SECONDS + CTA_SECONDS + 5:
        lines.append(f"Dialogue: 1,{ass_time(total - CTA_SECONDS)},{ass_time(total)},Cta,,0,0,0,,{cta}")
    for c in chunk_words(words, max_chars=16, max_words=3, max_sec=1.4):
        group = [w for w in words if c["start"] <= w["start"] <= c["end"]]
        for k, w in enumerate(group):
            end = group[k + 1]["start"] if k + 1 < len(group) else c["end"] + 0.08
            parts = [(r"{\c&H00D7FF&}" + _clean(x["word"]).upper() + r"{\c&HFFFFFF&}") if x is w
                     else _clean(x["word"]).upper() for x in group]
            pop = r"{\fscx108\fscy108\t(0,80,\fscx100\fscy100)}" if k == 0 else ""
            lines.append(f"Dialogue: 0,{ass_time(w['start'])},{ass_time(end)},Default,,0,0,0,,{pop}{' '.join(parts)}")
    out.write_text("\n".join(lines), encoding="utf-8")
    return out


def _filter_path(p: Path) -> str:
    # Rutas relativas: sin "C:" que el parser de filtros de ffmpeg confunde con un separador
    return os.path.relpath(p).replace("\\", "/").replace(":", "\\:")


def ass_filter(subs: Path) -> str:
    visuals.font("display", 40)  # garantiza que Anton esta descargada para libass
    return f"ass='{_filter_path(subs)}':fontsdir='{_filter_path(visuals.FONTS_DIR)}'"


def render_final(track: Path, audio: Path, subs: Path, duration: float, out: Path, crf: int = 21,
                 vertical: bool = False) -> Path:
    """Subtitulos + marca del canal (arriba a la derecha en 16:9, arriba centrada en 9:16)."""
    badge = visuals.brand_badge(46 if vertical else 40)
    pos = "x=(W-w)/2:y=120" if vertical else "x=W-w-36:y=30"
    run(["ffmpeg", "-y", "-loglevel", "error", "-i", str(track), "-i", str(audio), "-i", str(badge),
         "-filter_complex", f"[0:v]{ass_filter(subs)}[s];[s][2:v]overlay={pos}[vout]",
         "-map", "[vout]", "-map", "1:a",
         "-c:v", "libx264", "-preset", "veryfast", "-crf", str(crf), "-pix_fmt", "yuv420p",
         # Estereo a 48 kHz (el formato recomendado por Instagram): con la voz en mono a 44,1 kHz
         # Meta rechazaba a ratos los Reels con ProcessingFailedError
         "-c:a", "aac", "-b:a", "192k", "-ar", "48000", "-ac", "2",
         "-t", f"{duration:.3f}", "-movflags", "+faststart", str(out)],
        f"render final -> {out.name}")
    return out
