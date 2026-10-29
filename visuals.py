"""
================================================================================
 visuals.py - Motor de visuales de datos (graficos, cifras, comparativas)
================================================================================

 Convierte los "visuals" del guion en clips MP4 animados con DATOS REALES:
   chart    -> precio de Yahoo Finance que se dibuja en pantalla + % de cambio
   fred     -> serie macro de la FRED (hipotecas, inflacion, paro...)
   stat     -> cifra gigante con contador animado y flecha arriba/abajo
   compare  -> barras horizontales que crecen
   headline -> tarjeta de titular con la fuente

 Funciona en 16:9 (1920x1080, video largo) y 9:16 (1080x1920, Shorts).
 Los frames se dibujan con Pillow y se envian a ffmpeg por tuberia; tras la
 animacion el ultimo frame se mantiene (tpad), asi un clip de 6s cuesta ~2s.
 Coste: 0 EUR.
================================================================================
"""

import os
import re
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests
from PIL import Image, ImageDraw, ImageFilter, ImageFont

FPS        = 30
ANIM_SEC   = 1.4
FONTS_DIR  = Path("data/fonts")

BG         = (9, 13, 22)
BG_2       = (20, 28, 44)
GRID       = (38, 48, 66)
TEXT       = (240, 244, 250)
MUTED      = (140, 152, 172)
UP         = (0, 214, 125)
DOWN       = (255, 77, 79)
NEUTRAL    = (59, 160, 255)
GOLD       = (255, 196, 0)

FRED_API_KEY = os.getenv("FRED_API_KEY")

FRED_SERIES = {
    "MORTGAGE30US": ("30-year mortgage rate", "lin", "%"),
    "DGS10":        ("10-year Treasury yield", "lin", "%"),
    "DFF":          ("Fed funds rate", "lin", "%"),
    "CPIAUCSL":     ("Inflation (CPI, year over year)", "pc1", "%"),
    "UNRATE":       ("US unemployment rate", "lin", "%"),
    "UMCSENT":      ("Consumer sentiment", "lin", ""),
    "PSAVERT":      ("Personal saving rate", "lin", "%"),
    "CSUSHPINSA":   ("US home prices, year over year", "pc1", "%"),
    "GASREGW":      ("US gas price per gallon", "lin", "$"),
    "T10Y2Y":       ("10Y minus 2Y Treasury spread", "lin", "%"),
    "REVOLSL":      ("US credit card debt ($B)", "lin", "$B"),
    "MSPUS":        ("Median US home price", "lin", "$"),
}
PERIOD_DAYS = {"5d": 7, "1mo": 31, "6mo": 183, "1y": 366, "5y": 1830, "10y": 3660}


# ---------------------------------------------------------------------------
# Fuentes
# ---------------------------------------------------------------------------
_FONT_CANDIDATES = {
    "display": [FONTS_DIR / "Anton-Regular.ttf", Path("C:/Windows/Fonts/impact.ttf"),
                Path("/usr/share/fonts/truetype/dejavu/DejaVuSansCondensed-Bold.ttf")],
    "bold":    [Path("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"),
                Path("C:/Windows/Fonts/arialbd.ttf")],
    "regular": [Path("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"),
                Path("C:/Windows/Fonts/arial.ttf")],
}
_font_cache: dict = {}


_anton_tried = False


def _ensure_anton() -> None:
    global _anton_tried
    path = FONTS_DIR / "Anton-Regular.ttf"
    if _anton_tried or (path.exists() and path.stat().st_size > 10000):
        return
    _anton_tried = True
    FONTS_DIR.mkdir(parents=True, exist_ok=True)
    try:
        r = requests.get("https://github.com/google/fonts/raw/main/ofl/anton/Anton-Regular.ttf", timeout=30)
        r.raise_for_status()
        path.write_bytes(r.content)
    except Exception as e:
        print(f"   ! no pude bajar Anton: {e}")


def font(kind: str, size: int) -> ImageFont.FreeTypeFont:
    key = (kind, size)
    if key not in _font_cache:
        if kind == "display":
            _ensure_anton()
        for p in _FONT_CANDIDATES[kind]:
            if p.exists():
                _font_cache[key] = ImageFont.truetype(str(p), size)
                break
        else:
            _font_cache[key] = ImageFont.load_default(size)
    return _font_cache[key]


# ---------------------------------------------------------------------------
# Escritura de video (frames -> ffmpeg)
# ---------------------------------------------------------------------------
def write_clip(frames, size: tuple[int, int], duration: float, out_path: Path) -> None:
    """frames: lista de PIL.Image (animacion). El ultimo se congela hasta 'duration'."""
    w, h = size
    anim_sec = len(frames) / FPS
    hold = max(0.0, duration - anim_sec)
    proc = subprocess.Popen([
        "ffmpeg", "-y", "-loglevel", "error",
        "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f"{w}x{h}", "-r", str(FPS), "-i", "-",
        "-vf", f"tpad=stop_mode=clone:stop_duration={hold:.3f}",
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-pix_fmt", "yuv420p",
        "-r", str(FPS), "-t", f"{duration:.3f}", "-an", str(out_path),
    ], stdin=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        for f in frames:
            proc.stdin.write(f.convert("RGB").tobytes())
        proc.stdin.close()
    except BrokenPipeError:
        pass
    err = proc.stderr.read().decode(errors="ignore")
    if proc.wait() != 0:
        raise RuntimeError(f"ffmpeg visual fallo: {err[-300:]}")


def ease(t: float) -> float:
    t = max(0.0, min(1.0, t))
    return 1 - (1 - t) ** 3


def background(size: tuple[int, int]) -> Image.Image:
    """Fondo oscuro con un degradado radial suave (no plano)."""
    w, h = size
    small = Image.new("RGB", (64, 64), BG)
    d = ImageDraw.Draw(small)
    for r in range(40, 0, -1):
        k = r / 40
        col = tuple(int(BG[i] + (BG_2[i] - BG[i]) * (1 - k)) for i in range(3))
        d.ellipse([32 - r * 1.3, 20 - r, 32 + r * 1.3, 20 + r], fill=col)
    return small.resize((w, h), Image.BICUBIC).filter(ImageFilter.GaussianBlur(4))


def text_w(draw, text, f) -> int:
    b = draw.textbbox((0, 0), text, font=f)
    return b[2] - b[0]


def wrap(draw, text: str, f, max_w: int) -> list[str]:
    lines, cur = [], ""
    for word in text.split():
        test = f"{cur} {word}".strip()
        if text_w(draw, test, f) <= max_w or not cur:
            cur = test
        else:
            lines.append(cur)
            cur = word
    return lines + ([cur] if cur else [])


def fmt_value(v: float, unit: str = "") -> str:
    if unit == "%":
        return f"{v:.2f}%"
    if unit == "$B":
        return f"${v:,.0f}B"
    if abs(v) >= 1e12:
        s = f"{v/1e12:.2f}T"
    elif abs(v) >= 1e9:
        s = f"{v/1e9:.2f}B"
    elif abs(v) >= 1e6:
        s = f"{v/1e6:.2f}M"
    elif abs(v) >= 10000:
        s = f"{v:,.0f}"
    elif abs(v) >= 100:
        s = f"{v:,.1f}"
    else:
        s = f"{v:,.2f}"
    return f"${s}" if unit == "$" else s


# ---------------------------------------------------------------------------
# Datos
# ---------------------------------------------------------------------------
_data_cache: dict = {}


def fetch_ticker(ticker: str, period: str) -> list[tuple[datetime, float]] | None:
    key = ("yf", ticker, period)
    if key in _data_cache:
        return _data_cache[key]
    try:
        import yfinance as yf
        interval = {"5d": "30m", "1mo": "1d", "6mo": "1d", "1y": "1d", "5y": "1wk"}.get(period, "1d")
        df = yf.Ticker(ticker).history(period=period if period in PERIOD_DAYS else "1y", interval=interval)
        pts = [(idx.to_pydatetime(), float(v)) for idx, v in df["Close"].dropna().items()]
    except Exception as e:
        print(f"     ! yfinance {ticker}: {e}")
        pts = []
    _data_cache[key] = pts if len(pts) >= 5 else None
    return _data_cache[key]


def fetch_fred(series: str, period: str) -> list[tuple[datetime, float]] | None:
    key = ("fred", series, period)
    if key in _data_cache:
        return _data_cache[key]
    pts = None
    if FRED_API_KEY and series in FRED_SERIES:
        start = datetime.now(timezone.utc) - timedelta(days=PERIOD_DAYS.get(period, 1830))
        try:
            r = requests.get("https://api.stlouisfed.org/fred/series/observations", params={
                "series_id": series, "api_key": FRED_API_KEY, "file_type": "json",
                "observation_start": start.strftime("%Y-%m-%d"), "units": FRED_SERIES[series][1],
            }, timeout=20)
            r.raise_for_status()
            pts = [(datetime.strptime(o["date"], "%Y-%m-%d"), float(o["value"]))
                   for o in r.json().get("observations", []) if o.get("value") not in (".", "")]
        except Exception as e:
            print(f"     ! FRED {series}: {e}")
    _data_cache[key] = pts if pts and len(pts) >= 4 else None
    return _data_cache[key]


def downsample(points, n: int = 220):
    if len(points) <= n:
        return points
    step = len(points) / n
    return [points[int(i * step)] for i in range(n)] + [points[-1]]


# ---------------------------------------------------------------------------
# Render: grafico de linea
# ---------------------------------------------------------------------------
def render_line_chart(points, title: str, unit: str, size, duration, out_path: Path,
                      subtitle: str = "") -> None:
    w, h = size
    vertical = h > w
    s = min(w, h) / 1080
    points = downsample(points)
    vals = [v for _, v in points]
    first, last = vals[0], vals[-1]
    color = UP if last >= first else DOWN

    pad_l, pad_r = int(70 * s), int(160 * s)
    # Margenes que dejan libre la zona de subtitulos (abajo en 16:9, tercio inferior en 9:16)
    top = int((600 if vertical else 250) * s)
    bottom = h - int((640 if vertical else 210) * s)
    left, right = pad_l, w - pad_r
    lo, hi = min(vals), max(vals)
    span = (hi - lo) or abs(hi) * 0.05 or 1
    lo, hi = lo - span * 0.08, hi + span * 0.12

    def xy(i, v):
        x = left + (right - left) * i / (len(vals) - 1)
        y = bottom - (bottom - top) * (v - lo) / (hi - lo)
        return x, y

    base = background(size)
    d = ImageDraw.Draw(base)
    # Titulo
    tf = font("bold", int(46 * s))
    y_title = int((150 if vertical else 60) * s)
    for line in wrap(d, title, tf, w - 2 * pad_l):
        d.text((pad_l, y_title), line, font=tf, fill=TEXT)
        y_title += int(58 * s)
    if subtitle:
        d.text((pad_l, y_title + int(4 * s)), subtitle, font=font("regular", int(30 * s)), fill=MUTED)
    # Rejilla + etiquetas eje Y
    lf = font("regular", int(26 * s))
    for k in range(5):
        v = lo + (hi - lo) * k / 4
        _, y = xy(0, v)
        d.line([(left, y), (right, y)], fill=GRID, width=max(1, int(2 * s)))
        d.text((right + int(14 * s), y - int(15 * s)), fmt_value(v, unit), font=lf, fill=MUTED)
    # Fechas
    fmt = "%b %d" if (points[-1][0] - points[0][0]).days < 200 else "%b %Y"
    d.text((left, bottom + int(16 * s)), points[0][0].strftime(fmt), font=lf, fill=MUTED)
    end_lbl = points[-1][0].strftime(fmt)
    d.text((right - text_w(d, end_lbl, lf), bottom + int(16 * s)), end_lbl, font=lf, fill=MUTED)

    n_frames = int(ANIM_SEC * FPS)
    ss = 2  # supersampling para lineas suaves
    frames = []
    for fi in range(n_frames + 1):
        t = ease(fi / n_frames)
        upto = max(2, int(t * (len(vals) - 1)) + 1)
        layer = Image.new("RGBA", (w * ss, h * ss), (0, 0, 0, 0))
        ld = ImageDraw.Draw(layer)
        pts = [(x * ss, y * ss) for x, y in (xy(i, vals[i]) for i in range(upto))]
        ld.polygon(pts + [(pts[-1][0], bottom * ss), (pts[0][0], bottom * ss)], fill=color + (45,))
        ld.line(pts, fill=color + (255,), width=int(7 * s * ss), joint="curve")
        ex, ey = pts[-1]
        r = int(13 * s * ss)
        ld.ellipse([ex - r, ey - r, ex + r, ey + r], fill=color + (255,))
        layer = layer.resize((w, h), Image.LANCZOS)
        frame = Image.alpha_composite(base.convert("RGBA"), layer)
        fd = ImageDraw.Draw(frame)
        # Valor actual + cambio
        cur = vals[upto - 1]
        big = font("display", int(110 * s))
        val_txt = fmt_value(cur, unit)
        chg = (cur - first) / abs(first) * 100 if first else 0
        chg_txt = f"{'▲' if chg >= 0 else '▼'} {abs(chg):.1f}%"
        if vertical:
            x_val, y_val = pad_l, int(340 * s)
        else:
            x_val, y_val = w - pad_r - text_w(fd, val_txt, big) + int(120 * s), int(40 * s)
        fd.text((x_val, y_val), val_txt, font=big, fill=TEXT)
        cf = font("bold", int(44 * s))
        fd.text((x_val, y_val + int(130 * s)), chg_txt, font=cf, fill=UP if chg >= 0 else DOWN)
        frames.append(frame)
    write_clip(frames, size, duration, out_path)


# ---------------------------------------------------------------------------
# Render: cifra gigante
# ---------------------------------------------------------------------------
def _parse_number(value: str):
    m = re.search(r"(-?\d[\d,]*\.?\d*)", value)
    if not m:
        return None
    raw = m.group(1)
    decimals = len(raw.split(".")[1]) if "." in raw else 0
    return float(raw.replace(",", "")), decimals, value[:m.start()], value[m.end():], "," in raw


def render_stat(value: str, label: str, direction: str, size, duration, out_path: Path) -> None:
    w, h = size
    s = min(w, h) / 1080
    color = {"up": UP, "down": DOWN}.get(direction, GOLD)
    arrow = {"up": "▲", "down": "▼"}.get(direction, "")
    parsed = _parse_number(value)
    base = background(size)

    big = font("display", int((300 if h > w else 260) * s))
    while text_w(ImageDraw.Draw(base), value, big) > w * 0.86 and big.size > 60:
        big = font("display", big.size - 10)
    lf = font("bold", int(52 * s))
    frames = []
    n_frames = int(ANIM_SEC * FPS)
    for fi in range(n_frames + 1):
        t = ease(fi / n_frames)
        frame = base.copy()
        d = ImageDraw.Draw(frame)
        if parsed:
            num, dec, pre, post, comma = parsed
            cur = num * t
            shown = f"{pre}{cur:,.{dec}f}{post}" if comma else f"{pre}{cur:.{dec}f}{post}"
        else:
            shown = value
        tw = text_w(d, shown, big)
        bb = d.textbbox((0, 0), shown, font=big)
        y = h // 2 - (bb[3] - bb[1]) // 2 - int(80 * s)
        d.text(((w - tw) // 2 - bb[0], y - bb[1]), shown, font=big, fill=color)
        if arrow:
            af = font("bold", int(80 * s))
            d.text(((w - text_w(d, arrow, af)) // 2, y - int(130 * s)), arrow, font=af, fill=color)
        ly = y + (bb[3] - bb[1]) + int(50 * s)
        for line in wrap(d, label.upper(), lf, int(w * 0.8)):
            alpha = int(255 * min(1, t * 1.5))
            d.text(((w - text_w(d, line, lf)) // 2, ly), line, font=lf, fill=tuple(int(c * alpha / 255) for c in TEXT))
            ly += int(64 * s)
        frames.append(frame)
    write_clip(frames, size, duration, out_path)


# ---------------------------------------------------------------------------
# Render: comparativa de barras
# ---------------------------------------------------------------------------
def render_compare(title: str, items: list[dict], size, duration, out_path: Path) -> None:
    w, h = size
    vertical = h > w
    s = min(w, h) / 1080
    vals = []
    for it in items:
        try:
            vals.append(abs(float(str(it.get("value", 0)).replace(",", "").replace("$", "").replace("%", ""))))
        except ValueError:
            vals.append(0.0)
    vmax = max(vals) or 1
    base = background(size)
    d = ImageDraw.Draw(base)
    tf = font("bold", int(54 * s))
    y = int((330 if vertical else 90) * s)
    for line in wrap(d, title, tf, int(w * 0.88)):
        d.text(((w - text_w(d, line, tf)) // 2, y), line, font=tf, fill=TEXT)
        y += int(66 * s)

    lf = font("bold", int(38 * s))
    vf = font("display", int(70 * s))
    left = int(80 * s)
    bar_max = w - left * 2 - int(220 * s)
    area_top = y + int(70 * s)
    area_h = (h - int((660 if vertical else 200) * s)) - area_top
    row_h = min(int(220 * s), area_h // max(1, len(items)))
    best = max(range(len(vals)), key=lambda i: vals[i])

    frames, n_frames = [], int(ANIM_SEC * FPS)
    for fi in range(n_frames + 1):
        t = ease(fi / n_frames)
        frame = base.copy()
        fd = ImageDraw.Draw(frame)
        for i, it in enumerate(items):
            ry = area_top + i * row_h
            fd.text((left, ry), str(it.get("label", ""))[:40], font=lf, fill=MUTED)
            bh = int(row_h * 0.38)
            by = ry + int(54 * s)
            bw = int(bar_max * (vals[i] / vmax) * t)
            col = UP if i == best else NEUTRAL
            fd.rounded_rectangle([left, by, left + max(bw, int(8 * s)), by + bh], radius=int(10 * s), fill=col)
            disp = str(it.get("display") or it.get("value"))
            fd.text((left + bw + int(20 * s), by + bh // 2 - int(44 * s)), disp, font=vf, fill=TEXT)
        frames.append(frame)
    write_clip(frames, size, duration, out_path)


# ---------------------------------------------------------------------------
# Render: tarjeta de titular
# ---------------------------------------------------------------------------
def render_headline(source: str, text: str, size, duration, out_path: Path) -> None:
    w, h = size
    s = min(w, h) / 1080
    base = background(size)
    card_w = int(w * (0.86 if h > w else 0.72))
    hf = font("bold", int(64 * s))
    lines = wrap(ImageDraw.Draw(base), text, hf, card_w - int(120 * s))[:5]
    line_h = int(80 * s)
    card_h = int(170 * s) + line_h * len(lines) + int(60 * s)
    cx, cy = (w - card_w) // 2, (h - card_h) // 2

    frames, n_frames = [], int(ANIM_SEC * FPS)
    words_total = sum(len(l.split()) for l in lines)
    for fi in range(n_frames + 1):
        t = ease(fi / n_frames)
        frame = base.copy()
        offset = int((1 - t) * 80 * s)
        shadow = Image.new("RGBA", size, (0, 0, 0, 0))
        ImageDraw.Draw(shadow).rounded_rectangle(
            [cx + 10, cy + offset + 16, cx + card_w + 10, cy + offset + card_h + 16], radius=int(26 * s), fill=(0, 0, 0, 140))
        frame = Image.alpha_composite(frame.convert("RGBA"), shadow.filter(ImageFilter.GaussianBlur(14)))
        d = ImageDraw.Draw(frame)
        d.rounded_rectangle([cx, cy + offset, cx + card_w, cy + offset + card_h], radius=int(26 * s), fill=(250, 250, 252))
        pf = font("bold", int(34 * s))
        pill = (source or "NEWS").upper()
        pw = text_w(d, pill, pf) + int(40 * s)
        d.rounded_rectangle([cx + int(60 * s), cy + offset + int(50 * s), cx + int(60 * s) + pw, cy + offset + int(105 * s)],
                            radius=int(26 * s), fill=DOWN)
        d.text((cx + int(80 * s), cy + offset + int(56 * s)), pill, font=pf, fill=(255, 255, 255))
        shown_words = int(words_total * min(1, t * 1.3)) + 1
        y = cy + offset + int(140 * s)
        for line in lines:
            ws = line.split()
            take, shown_words = ws[:max(0, shown_words)], shown_words - len(ws)
            d.text((cx + int(60 * s), y), " ".join(take), font=hf, fill=(15, 18, 25))
            y += line_h
        frames.append(frame)
    write_clip(frames, size, duration, out_path)


# ---------------------------------------------------------------------------
# API publica
# ---------------------------------------------------------------------------
PERIOD_LABEL = {"5d": "last 5 days", "1mo": "last month", "6mo": "last 6 months",
                "1y": "last 12 months", "5y": "last 5 years", "10y": "last 10 years"}


def render_visual(beat: dict, size: tuple[int, int], duration: float, out_path: Path) -> bool:
    """Renderiza un visual del guion. Devuelve False si no hay datos (se usa b-roll)."""
    kind = beat.get("type")
    try:
        if kind == "chart":
            ticker = str(beat.get("ticker", "")).upper()
            period = beat.get("period", "1y")
            pts = fetch_ticker(ticker, period)
            if not pts:
                return False
            title = beat.get("label") or ticker
            # Indices (^GSPC) y divisas (EURUSD=X) no son precios en dolares
            unit = "" if ticker.startswith("^") or ticker.endswith("=X") else "$"
            render_line_chart(pts, title, unit, size, duration, out_path,
                              subtitle=f"{ticker} · {PERIOD_LABEL.get(period, period)} · Yahoo Finance")
        elif kind == "fred":
            series = beat.get("series", "")
            period = beat.get("period", "5y")
            pts = fetch_fred(series, period)
            if not pts:
                return False
            name, _units, unit = FRED_SERIES[series]
            render_line_chart(pts, beat.get("label") or name, unit, size, duration, out_path,
                              subtitle=f"{PERIOD_LABEL.get(period, period)} · Source: FRED, St. Louis Fed")
        elif kind == "stat":
            render_stat(str(beat.get("value", "")), str(beat.get("label", "")),
                        str(beat.get("direction", "neutral")), size, duration, out_path)
        elif kind == "compare":
            items = [i for i in beat.get("items", []) if isinstance(i, dict)][:4]
            if len(items) < 2:
                return False
            render_compare(str(beat.get("title", "")), items, size, duration, out_path)
        elif kind == "headline":
            if not beat.get("text"):
                return False
            render_headline(str(beat.get("source", "")), str(beat["text"]), size, duration, out_path)
        else:
            return False
    except Exception as e:
        print(f"     ! visual {kind} fallo: {e}")
        return False
    return out_path.exists() and out_path.stat().st_size > 0


DEFAULT_DURATION = {"chart": 6.0, "fred": 6.0, "stat": 3.6, "compare": 5.5, "headline": 4.5}
