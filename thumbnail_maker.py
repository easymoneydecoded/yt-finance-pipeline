"""
================================================================================
 thumbnail_maker.py - Etapa 7 (v3, MAXIMA NITIDEZ, DISEÑO AGRESIVO)
================================================================================
 Mejoras v3:
   - Re-descarga el clip elegido en HD (Full HD) desde Pexels para el frame
   - Guarda en PNG (texto sin halo de compresion JPEG)
   - Control de peso: si el PNG supera 2MB, optimiza para caber en YouTube
   - Diseño Minimalista Agresivo (Eliminación de barras, texto gigante, sombras)
 Mejoras v5 (miniaturas con datos):
   - Layout "chart": el grafico real del video (verde/rojo) + flecha + texto
   - Layout "bignum": la cifra clave gigante con flecha sobre fondo desenfocado
   - Se prefieren cuando el guion tiene datos; el aprendizaje favorece el
     layout que mejor rinde en el canal
 Mejoras v4 (anti-repeticion):
   - 4 layouts x 6 colores de acento; se evita el combo de los ultimos videos
   - El clip de fondo no puede ser uno usado en miniaturas recientes
   - Frame elegido en un instante aleatorio del clip
   - Guarda thumbnail_meta_YYYY-MM-DD.json para que el uploader lo registre
================================================================================
"""

import os
import re
import json
import random
import subprocess
from pathlib import Path
from datetime import datetime, timezone

import requests
from PIL import Image, ImageDraw, ImageFont, ImageFilter, ImageEnhance, ImageOps
from dotenv import load_dotenv

import content_history as ch
import visuals

load_dotenv()

INPUT_DIR      = Path("data")
BROLL_DIR      = Path("data/broll")
OUTPUT_DIR     = Path("data/output")
FONTS_DIR      = Path("data/fonts")
TEMP_DIR       = Path("data/tmp_thumb")
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
FONTS_DIR.mkdir(parents=True, exist_ok=True)
TEMP_DIR.mkdir(parents=True, exist_ok=True)

TODAY          = datetime.now(timezone.utc).strftime("%Y-%m-%d")

PEXELS_API_KEY = os.getenv("PEXELS_API_KEY")

THUMB_W        = 1280
THUMB_H        = 720

COLOR_WHITE    = (255, 255, 255)
COLOR_BLACK    = (0, 0, 0)

ACCENT_COLORS = {
    "orange": (255, 107, 0),
    "yellow": (255, 214, 0),
    "red":    (235, 36, 50),
    "green":  (0, 220, 110),
    "cyan":   (0, 200, 255),
    "pink":   (255, 60, 170),
}
LAYOUTS = ["center", "left", "box", "bottom"]
DATA_LAYOUTS = ["chart", "bignum"]
COLOR_UP   = (0, 220, 110)
COLOR_DOWN = (235, 36, 50)

OVERLAY_ALPHA  = 115

# YouTube limite de miniatura: 2MB
MAX_THUMB_BYTES = 2 * 1024 * 1024

ANTON_URL      = "https://github.com/google/fonts/raw/main/ofl/anton/Anton-Regular.ttf"
ANTON_PATH     = FONTS_DIR / "Anton-Regular.ttf"
DEJAVU_BOLD    = Path("/usr/share/fonts/truetype/dejavu/DejaVuSansCondensed-Bold.ttf")
DEJAVU_FALLBACK = Path("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf")


def load_data() -> tuple[dict, dict]:
    def _latest(pattern: str, base: Path = INPUT_DIR) -> Path:
        candidates = sorted(base.rglob(pattern), reverse=True)
        if not candidates:
            raise FileNotFoundError(f"No se encontro {pattern}")
        return candidates[0]

    script_path = INPUT_DIR / f"script_{TODAY}.json"
    if not script_path.exists():
        script_path = _latest("script_*.json")
    broll_path = BROLL_DIR / f"broll_manifest_{TODAY}.json"
    if not broll_path.exists():
        broll_path = _latest("broll_manifest_*.json")

    print(f"   . script: {script_path.name}")
    print(f"   . broll:  {broll_path.name}")
    return (
        json.loads(script_path.read_text(encoding="utf-8")),
        json.loads(broll_path.read_text(encoding="utf-8")),
    )


def download_font_if_missing() -> Path:
    if ANTON_PATH.exists() and ANTON_PATH.stat().st_size > 10000:
        return ANTON_PATH
    try:
        print(f"   . descargando fuente Anton...")
        r = requests.get(ANTON_URL, timeout=30)
        r.raise_for_status()
        ANTON_PATH.write_bytes(r.content)
        if ANTON_PATH.stat().st_size > 10000:
            return ANTON_PATH
    except Exception as e:
        print(f"   ! fallo descarga Anton: {e}")
    if DEJAVU_BOLD.exists():
        return DEJAVU_BOLD
    if DEJAVU_FALLBACK.exists():
        return DEJAVU_FALLBACK
    raise RuntimeError("No hay fuente disponible")


def pick_style(history: list[dict], rng: random.Random, has_chart: bool = False,
               has_number: bool = False) -> tuple[str, str]:
    """
    Layout y color distintos de los de las ultimas miniaturas. Los layouts con
    datos pesan el doble cuando hay datos; el rendimiento real ajusta el resto.
    """
    recent_layouts = set(ch.recent_values(history, "thumb_layout", last_n=2))
    recent_colors  = ch.recent_values(history, "thumb_color", last_n=3)
    data = (["chart"] if has_chart else []) + (["bignum"] if has_number else [])
    options = data * 2 + LAYOUTS
    layout = ch.weighted_choice(options, history, "thumb_layout", rng=rng, exclude=recent_layouts)
    colors  = [c for c in ACCENT_COLORS if c not in recent_colors] or list(ACCENT_COLORS)
    return layout, rng.choice(colors)


def thumbnail_data(script_data: dict, thumbnail_text: str) -> dict:
    """Cifra clave, direccion y puntos del grafico principal del video."""
    beats = [v for s in script_data.get("sections", []) for v in s.get("visuals", []) if isinstance(v, dict)]
    _, number = split_number_from_text(thumbnail_text.upper())
    stat = next((b for b in beats if b.get("type") == "stat"), None)
    if not number and stat:
        number = str(stat.get("value", ""))
    direction = (stat or {}).get("direction", "neutral")

    points = None
    for b in beats:
        if b.get("type") == "chart":
            points = visuals.fetch_ticker(str(b.get("ticker", "")).upper(), b.get("period", "1y"))
        elif b.get("type") == "fred":
            points = visuals.fetch_fred(b.get("series", ""), b.get("period", "5y"))
        if points:
            if direction == "neutral":
                direction = "up" if points[-1][1] >= points[0][1] else "down"
            break
    return {"number": number, "direction": direction, "points": points}


def pick_best_broll_clip(broll_manifest: dict, used_ids: set, rng: random.Random) -> dict:
    """Devuelve el dict completo del clip (con pexels_id) para poder re-bajar en HD."""
    body_clips = []
    all_clips = []
    for section in broll_manifest.get("sections", []):
        stype = section.get("section_type")
        for clip in section.get("clips", []):
            clip_path = INPUT_DIR / clip["filename"]
            if not clip_path.exists():
                continue
            all_clips.append(clip)
            if stype == "body":
                body_clips.append(clip)
    pool = body_clips or all_clips
    if not pool:
        raise RuntimeError("No hay clips de b-roll disponibles")
    fresh = [c for c in pool if c.get("pexels_id") not in used_ids] or pool
    good = [c for c in fresh if c.get("duration_sec", 0) >= 10]
    chosen_pool = good or fresh
    return rng.choice(chosen_pool)


def download_hd_clip(pexels_id: int) -> Path | None:
    """Re-descarga el clip en la mayor resolucion disponible desde Pexels."""
    if not PEXELS_API_KEY or not pexels_id:
        return None
    try:
        r = requests.get(
            f"https://api.pexels.com/videos/videos/{pexels_id}",
            headers={"Authorization": PEXELS_API_KEY},
            timeout=30,
        )
        if r.status_code != 200:
            print(f"   ! Pexels HD devolvio {r.status_code}, uso clip local")
            return None
        data = r.json()
        # Elegir el video_file de mayor resolucion (mas cercano a 1920 de ancho)
        files = [f for f in data.get("video_files", []) if f.get("file_type") == "video/mp4"]
        if not files:
            return None
        # Preferir el mas cercano a 1920px de ancho (Full HD)
        files.sort(key=lambda f: abs(f.get("width", 0) - 1920))
        best = files[0]
        hd_path = TEMP_DIR / f"hd_clip.mp4"
        with requests.get(best["link"], stream=True, timeout=120) as resp:
            resp.raise_for_status()
            with open(hd_path, "wb") as fh:
                for chunk in resp.iter_content(chunk_size=65536):
                    if chunk:
                        fh.write(chunk)
        print(f"   . clip HD descargado: {best.get('width')}x{best.get('height')}")
        return hd_path
    except Exception as e:
        print(f"   ! fallo descarga HD: {e}, uso clip local")
        return None


def extract_frame(clip_path: Path, out_path: Path, offset_sec: float = 3.0) -> None:
    result = subprocess.run([
        "ffmpeg", "-y", "-loglevel", "error",
        "-ss", f"{offset_sec:.2f}",
        "-i", str(clip_path),
        "-vframes", "1",
        "-q:v", "1",
        str(out_path),
    ], capture_output=True, text=True)
    if result.returncode != 0 or not out_path.exists():
        subprocess.run([
            "ffmpeg", "-y", "-loglevel", "error",
            "-i", str(clip_path),
            "-vframes", "1",
            "-q:v", "1",
            str(out_path),
        ], check=True)


def crop_center(img: Image.Image, target_w: int, target_h: int) -> Image.Image:
    src_w, src_h = img.size
    src_ratio = src_w / src_h
    dst_ratio = target_w / target_h
    if src_ratio > dst_ratio:
        new_h = target_h
        new_w = int(src_w * (target_h / src_h))
        img = img.resize((new_w, new_h), Image.LANCZOS)
        left = (new_w - target_w) // 2
        img = img.crop((left, 0, left + target_w, target_h))
    else:
        new_w = target_w
        new_h = int(src_h * (target_w / src_w))
        img = img.resize((new_w, new_h), Image.LANCZOS)
        top = (new_h - target_h) // 2
        img = img.crop((0, top, target_w, top + target_h))

    # Mejoras de calidad
    img = img.filter(ImageFilter.UnsharpMask(radius=2.5, percent=140, threshold=2))
    img = ImageEnhance.Contrast(img).enhance(1.10)
    img = ImageEnhance.Color(img).enhance(1.12)
    return img


def wrap_text_for_font(text, font, max_width, draw):
    words = text.split()
    lines, current = [], ""
    for word in words:
        test = f"{current} {word}".strip()
        bbox = draw.textbbox((0, 0), test, font=font)
        if (bbox[2] - bbox[0]) <= max_width or not current:
            current = test
        else:
            lines.append(current)
            current = word
    if current:
        lines.append(current)
    return lines


def draw_text_with_shadow(draw, xy, text, font, fill, stroke=False):
    x, y = xy
    if stroke:
        # Contorno negro grueso: lee bien sobre cualquier fondo
        draw.text((x, y), text, font=font, fill=fill, stroke_width=7, stroke_fill=COLOR_BLACK)
        return
    # Modern drop shadow: draw text slightly offset in black, then main text
    shadow_color = (0, 0, 0, 180)
    draw.text((x + 6, y + 6), text, font=font, fill=shadow_color)
    draw.text((x, y), text, font=font, fill=fill)


def split_number_from_text(text):
    for pat in [r'\$[\d,\.]+[KMBT]?', r'\d+[\.,]?\d*\s*%', r'\d+[KMBT]']:
        m = re.search(pat, text)
        if m:
            return (text[:m.start()] + text[m.end():]).strip(), m.group().strip()
    return text, ""


def fit_font(lines_text, font_path, max_w, max_h, draw, start=150, minimum=80, extra_lines=0):
    """Fuente lo mas grande posible que quepa en max_w x max_h."""
    size = start
    while True:
        font = ImageFont.truetype(str(font_path), size)
        lines = wrap_text_for_font(lines_text, font, max_w, draw) if lines_text else []
        widest = max((draw.textbbox((0, 0), l, font=font)[2] for l in lines), default=0)
        total_h = (len(lines) + extra_lines) * (size + 20)
        if (widest <= max_w and total_h <= max_h) or size <= minimum:
            return font, lines
        size -= 8


def gradient_overlay(direction: str, strong: int = 230, weak: int = 40) -> Image.Image:
    """Degradado negro: 'left' (oscuro a la izquierda) o 'bottom' (oscuro abajo)."""
    # linear_gradient va de negro (arriba) a blanco (abajo); girado -90 queda blanco a la izquierda
    col = Image.linear_gradient("L")
    if direction == "left":
        col = col.rotate(-90)
    col = col.resize((THUMB_W, THUMB_H)).point(lambda v: weak + (strong - weak) * v // 255)
    overlay = Image.new("RGBA", (THUMB_W, THUMB_H), (0, 0, 0, 0))
    overlay.putalpha(col)
    return overlay


def build_thumbnail(frame_path: Path, thumbnail_text: str, font_path: Path, output_path: Path,
                    layout: str = "center", accent=ACCENT_COLORS["orange"]) -> Path:
    bg = Image.open(frame_path).convert("RGB")
    bg = crop_center(bg, THUMB_W, THUMB_H)
    if layout in ("left", "box"):
        bg = ImageOps.mirror(bg) if random.random() < 0.5 else bg
    bg = bg.convert("RGBA")

    if layout == "left":
        bg = Image.alpha_composite(bg, gradient_overlay("left"))
    elif layout == "bottom":
        bg = Image.alpha_composite(bg, gradient_overlay("bottom", strong=235, weak=30))
    else:
        # Dramatizar el fondo: oscurecerlo para que el texto resalte
        bg = Image.alpha_composite(bg, Image.new("RGBA", (THUMB_W, THUMB_H), (0, 0, 0, 150)))

    draw = ImageDraw.Draw(bg)

    main_text = thumbnail_text.upper()
    text_before, big_number = split_number_from_text(main_text)
    margin = 70

    if layout == "center":
        font, lines = fit_font(text_before, font_path, THUMB_W - 100, THUMB_H - 80, draw,
                               extra_lines=1 if big_number else 0)
        line_h = font.size + 20
        y = (THUMB_H - (len(lines) + (1 if big_number else 0)) * line_h) // 2
        for line in lines:
            w = draw.textbbox((0, 0), line, font=font)[2]
            draw_text_with_shadow(draw, ((THUMB_W - w) // 2, y), line, font, COLOR_WHITE)
            y += line_h
        if big_number:
            w = draw.textbbox((0, 0), big_number, font=font)[2]
            draw_text_with_shadow(draw, ((THUMB_W - w) // 2, y), big_number, font, accent)

    elif layout == "left":
        # Barra de color vertical + texto alineado a la izquierda en la mitad oscura
        max_w = int(THUMB_W * 0.62)
        font, lines = fit_font(text_before, font_path, max_w, THUMB_H - 120, draw,
                               extra_lines=1 if big_number else 0)
        line_h = font.size + 20
        block_h = (len(lines) + (1 if big_number else 0)) * line_h
        y = (THUMB_H - block_h) // 2
        draw.rectangle([margin - 40, y, margin - 22, y + block_h - 20], fill=accent)
        if big_number:
            draw_text_with_shadow(draw, (margin, y), big_number, font, accent)
            y += line_h
        for line in lines:
            draw_text_with_shadow(draw, (margin, y), line, font, COLOR_WHITE)
            y += line_h

    elif layout == "box":
        # Texto con contorno arriba; numero (o ultima palabra) en caja de color abajo
        if not big_number and len(text_before.split()) > 1:
            *rest, last = text_before.split()
            text_before, big_number = " ".join(rest), last
        font, lines = fit_font(text_before, font_path, THUMB_W - 2 * margin, THUMB_H * 0.5, draw)
        y = margin
        for line in lines:
            draw_text_with_shadow(draw, (margin, y), line, font, COLOR_WHITE, stroke=True)
            y += font.size + 20
        if big_number:
            box_font, _ = fit_font(big_number, font_path, THUMB_W - 2 * margin - 60, 200, draw, start=170)
            bb = draw.textbbox((0, 0), big_number, font=box_font)
            bw, bh = bb[2] - bb[0], bb[3] - bb[1]
            bx, by = margin, THUMB_H - margin - bh - 50
            draw.rectangle([bx, by, bx + bw + 60, by + bh + 50], fill=accent)
            draw.text((bx + 30 - bb[0], by + 25 - bb[1]), big_number, font=box_font, fill=COLOR_BLACK)

    else:  # bottom
        # Texto abajo con contorno; la primera linea en color de acento
        font, lines = fit_font(text_before, font_path, THUMB_W - 2 * margin, THUMB_H * 0.55, draw,
                               extra_lines=1 if big_number else 0)
        line_h = font.size + 20
        all_lines = ([big_number] if big_number else []) + lines
        y = THUMB_H - margin - len(all_lines) * line_h + 20
        for n, line in enumerate(all_lines):
            color = accent if n == 0 else COLOR_WHITE
            draw_text_with_shadow(draw, (margin, y), line, font, color, stroke=True)
            y += line_h

    # Guardar como PNG (texto perfecto, sin halo)
    final = bg.convert("RGB")
    final.save(str(output_path), "PNG", optimize=True)

    # Control de peso: si supera 2MB, reducir progresivamente
    if output_path.stat().st_size > MAX_THUMB_BYTES:
        print(f"   . PNG pesa {output_path.stat().st_size/1024/1024:.1f}MB, optimizando...")
        # Reducir colores del fondo con quantize suave manteniendo texto
        for colors in [256, 192, 128]:
            final.quantize(colors=colors, method=Image.FASTOCTREE).convert("RGB").save(
                str(output_path), "PNG", optimize=True)
            if output_path.stat().st_size <= MAX_THUMB_BYTES:
                break
        # Si aun asi se pasa, fallback a JPG calidad alta
        if output_path.stat().st_size > MAX_THUMB_BYTES:
            jpg_path = output_path.with_suffix(".jpg")
            final.save(str(jpg_path), "JPEG", quality=94, optimize=True, subsampling=0)
            output_path.unlink()
            print(f"   . usando JPG de respaldo (PNG no cabia)")
            return jpg_path

    return output_path


def draw_arrow(draw, cx: int, cy: int, size: int, up: bool, color) -> None:
    """Flecha gruesa con contorno negro."""
    s, d = size, (-1 if up else 1)
    pts = [(cx, cy + d * s), (cx - s * 0.75, cy), (cx - s * 0.32, cy), (cx - s * 0.32, cy - d * s),
           (cx + s * 0.32, cy - d * s), (cx + s * 0.32, cy), (cx + s * 0.75, cy)]
    draw.polygon(pts, fill=color, outline=(0, 0, 0), width=8)


def _dark_bg(frame_path: Path, blur: float, darkness: int) -> Image.Image:
    bg = crop_center(Image.open(frame_path).convert("RGB"), THUMB_W, THUMB_H)
    if blur:
        bg = bg.filter(ImageFilter.GaussianBlur(blur))
    return Image.alpha_composite(bg.convert("RGBA"), Image.new("RGBA", (THUMB_W, THUMB_H), (0, 0, 0, darkness)))


def build_data_thumbnail(frame_path: Path, thumbnail_text: str, data: dict, font_path: Path,
                         output_path: Path, layout: str, accent) -> Path:
    direction = data.get("direction", "neutral")
    color = COLOR_UP if direction == "up" else COLOR_DOWN if direction == "down" else accent
    text_before, number = split_number_from_text(thumbnail_text.upper())
    number = number or data.get("number", "")
    text_before = text_before or thumbnail_text.upper()

    if layout == "chart" and data.get("points"):
        bg = Image.alpha_composite(_dark_bg(frame_path, 2, 175), gradient_overlay("left", strong=200, weak=0))
        layer = Image.new("RGBA", (THUMB_W * 2, THUMB_H * 2), (0, 0, 0, 0))
        ld = ImageDraw.Draw(layer)
        pts = visuals.downsample(data["points"], 90)
        vals = [v for _, v in pts]
        lo, hi = min(vals), max(vals)
        span = (hi - lo) or 1
        x0, x1, y0, y1 = 1100, 2480, 260, 1240
        line = [(x0 + (x1 - x0) * i / (len(vals) - 1), y1 - (y1 - y0) * (v - lo) / span) for i, v in enumerate(vals)]
        ld.polygon(line + [(x1, 1440), (x0, 1440)], fill=color + (70,))
        ld.line(line, fill=(0, 0, 0, 255), width=44, joint="curve")
        ld.line(line, fill=color + (255,), width=26, joint="curve")
        ex, ey = line[-1]
        ld.ellipse([ex - 34, ey - 34, ex + 34, ey + 34], fill=color + (255,), outline=(0, 0, 0, 255), width=8)
        bg = Image.alpha_composite(bg, layer.resize((THUMB_W, THUMB_H), Image.LANCZOS))
        draw = ImageDraw.Draw(bg)
        draw_arrow(draw, 1110, 150 if direction != "down" else 570, 110, direction != "down", color)

        font, lines = fit_font(text_before, font_path, 560, 360 if number else 560, draw, start=130, minimum=70)
        y = 60
        for ln in lines:
            draw_text_with_shadow(draw, (50, y), ln, font, COLOR_WHITE, stroke=True)
            y += font.size + 14
        if number:
            nf, _ = fit_font(number, font_path, 600, 240, draw, start=230, minimum=90)
            draw_text_with_shadow(draw, (50, max(y + 10, THUMB_H - nf.size - 90)), number, nf, color, stroke=True)

    else:  # bignum
        bg = _dark_bg(frame_path, 9, 150)
        draw = ImageDraw.Draw(bg)
        number = number or text_before.split()[-1]
        font, lines = fit_font(text_before, font_path, THUMB_W - 140, 210, draw, start=110, minimum=60)
        y = 50
        for ln in lines:
            w = draw.textbbox((0, 0), ln, font=font)[2]
            draw_text_with_shadow(draw, ((THUMB_W - w) // 2, y), ln, font, COLOR_WHITE, stroke=True)
            y += font.size + 12
        has_arrow = direction in ("up", "down")
        nf, _ = fit_font(number, font_path, THUMB_W - (420 if has_arrow else 140), THUMB_H - y - 60, draw,
                         start=400, minimum=120)
        bb = draw.textbbox((0, 0), number, font=nf)
        nw, nh = bb[2] - bb[0], bb[3] - bb[1]
        total_w = nw + (250 if has_arrow else 0)
        nx = (THUMB_W - total_w) // 2 + (250 if has_arrow else 0)
        ny = y + (THUMB_H - y - nh) // 2 - bb[1] - 10
        if has_arrow:
            draw_arrow(draw, nx - 140, ny + bb[1] + nh // 2, 115, direction == "up", color)
        draw.text((nx - bb[0], ny), number, font=nf, fill=color, stroke_width=10, stroke_fill=(0, 0, 0))

    final = bg.convert("RGB")
    final.save(str(output_path), "PNG", optimize=True)
    if output_path.stat().st_size > MAX_THUMB_BYTES:
        jpg_path = output_path.with_suffix(".jpg")
        final.save(str(jpg_path), "JPEG", quality=93, optimize=True, subsampling=0)
        output_path.unlink()
        return jpg_path
    return output_path


def cleanup_temp():
    import shutil
    try:
        shutil.rmtree(TEMP_DIR)
    except Exception:
        pass


def main() -> None:
    print(f"\n=== THUMBNAIL MAKER . {TODAY} ===\n")

    print("1/4  Cargando datos...")
    script_data, broll_manifest = load_data()
    thumbnail_text = script_data.get("thumbnail_text", "").strip()
    if not thumbnail_text:
        thumbnail_text = script_data.get("youtube_title", "MARKET UPDATE")[:30]
    print(f"      texto: '{thumbnail_text}'")

    print("\n2/4  Preparando fuente...")
    font_path = download_font_if_missing()

    history = ch.load_history()
    rng = random.Random(script_data.get("youtube_title", "") + TODAY)
    random.seed(rng.random())
    data = thumbnail_data(script_data, thumbnail_text)
    layout, color_name = pick_style(history, rng, has_chart=bool(data["points"]), has_number=bool(data["number"]))
    print(f"      datos: cifra={data['number'] or '-'} direccion={data['direction']} grafico={'si' if data['points'] else 'no'}")
    print(f"      estilo: layout={layout}, color={color_name}")

    print("\n3/4  Obteniendo frame en alta calidad...")
    used_ids = set(ch.recent_values(history, "thumb_pexels_id")) | set(ch.recent_values(history, "broll_pexels_ids"))
    clip = pick_best_broll_clip(broll_manifest, used_ids, rng)
    pexels_id = clip.get("pexels_id")
    local_clip = INPUT_DIR / clip["filename"]
    print(f"      clip: {local_clip.name} (pexels {pexels_id})")

    # Intentar bajar version HD; si falla, usar el clip local
    hd_clip = download_hd_clip(pexels_id)
    source_clip = hd_clip if hd_clip else local_clip

    frame_path = TEMP_DIR / "frame.png"
    max_offset = max(1.5, min(8.0, float(clip.get("duration_sec", 5)) - 1.0))
    extract_frame(source_clip, frame_path, offset_sec=rng.uniform(1.0, max_offset))

    print("\n4/4  Componiendo thumbnail (PNG)...")
    output_path = OUTPUT_DIR / f"thumbnail_{TODAY}.png"
    if layout in DATA_LAYOUTS:
        final_path = build_data_thumbnail(frame_path, thumbnail_text, data, font_path, output_path,
                                          layout, ACCENT_COLORS[color_name])
    else:
        final_path = build_thumbnail(frame_path, thumbnail_text, font_path, output_path,
                                     layout=layout, accent=ACCENT_COLORS[color_name])

    (OUTPUT_DIR / f"thumbnail_meta_{TODAY}.json").write_text(json.dumps({
        "thumbnail_text":  thumbnail_text,
        "thumb_layout":    layout,
        "thumb_color":     color_name,
        "thumb_pexels_id": pexels_id,
        "file":            final_path.name,
    }, indent=2), encoding="utf-8")

    cleanup_temp()

    size_kb = final_path.stat().st_size / 1024
    print(f"\n  Listo")
    print(f"     Thumbnail: {final_path}")
    print(f"     Tamano:    {size_kb:.1f} KB")


if __name__ == "__main__":
    main()
