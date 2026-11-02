"""
================================================================================
 insider_cards.py - Imagenes de los posts de Bluesky, X y Threads (1200x675)
================================================================================

 Tarjeta con los colores de tickerpress.net:
   diaria  : las 3 mayores compras del dia + grafico de 6 meses de la accion n.1
             con el precio de compra marcado
   semanal : cifras de la semana + barras de las acciones con mas compras
 Si algo falla (sin matplotlib, sin precios...), devuelve None y el post sale sin imagen.

 Uso: python insider_cards.py   (genera las dos de prueba en cards_preview/)
================================================================================
"""

from datetime import datetime, timedelta
from pathlib import Path

import insider_alerts as ia

BG, CARD, LINE, TEXT, MUTED, GREEN = "#090d16", "#131c2e", "#22304a", "#f5f7fa", "#96a2b6", "#00d67d"
OUT = Path("cards")
W, H = 12, 6.75                      # pulgadas a 100 ppp = 1200 x 675


def _fig():
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    plt.rcParams.update({"font.family": "DejaVu Sans", "text.color": TEXT, "axes.edgecolor": LINE,
                         "axes.labelcolor": MUTED, "xtick.color": MUTED, "ytick.color": MUTED})
    fig = plt.figure(figsize=(W, H), dpi=100, facecolor=BG)
    logo = fig.text(0.04, 0.925, "TICKER", fontsize=17, fontweight="bold", color=TEXT)
    box = logo.get_window_extent(renderer=fig.canvas.get_renderer())      # PRESS justo detras de TICKER
    fig.text(box.x1 / fig.bbox.width, 0.925, "PRESS", fontsize=17, fontweight="bold", color=GREEN)
    fig.text(0.04, 0.035, "Source: SEC Form 4 filings · Prices: Yahoo Finance · Not financial advice",
             fontsize=10, color=MUTED)
    fig.text(0.96, 0.035, "tickerpress.net", fontsize=12, color=GREEN, ha="right", fontweight="bold")
    return plt, fig


def _badge(fig, text: str) -> None:
    fig.text(0.96, 0.925, f" {text} ", fontsize=12, fontweight="bold", color=BG, ha="right",
             bbox={"facecolor": GREEN, "edgecolor": "none", "boxstyle": "round,pad=0.35"})


def _short(s: str, n: int) -> str:
    return s if len(s) <= n else s[:n - 1].rstrip() + "…"


def _save(plt, fig, name: str) -> Path:
    OUT.mkdir(exist_ok=True)
    path = OUT / name
    fig.savefig(path, facecolor=BG, dpi=100)
    plt.close(fig)
    return path


def daily_card(day: str, buys: list[dict]) -> Path | None:
    try:
        plt, fig = _fig()
        pretty = datetime.fromisoformat(day).strftime("%b %d, %Y").replace(" 0", " ")
        _badge(fig, "INSIDER BUYS")
        fig.text(0.04, 0.80, f"Insiders bought {ia.money(sum(b['value'] for b in buys))} of stock",
                 fontsize=27, fontweight="bold")
        fig.text(0.04, 0.745, f"{len(buys)} open-market purchases filed {pretty}", fontsize=14, color=MUTED)
        # Tabla de las 3 mayores
        y = 0.63
        for i, b in enumerate(buys[:3], 1):
            fig.text(0.04, y, f"{i}", fontsize=22, fontweight="bold", color=GREEN)
            fig.text(0.075, y + 0.008, f"${b['ticker']}", fontsize=19, fontweight="bold")
            fig.text(0.40, y + 0.008, ia.money(b["value"]), fontsize=19, fontweight="bold", color=GREEN, ha="right")
            fig.text(0.075, y - 0.045, _short(b["issuer"], 34), fontsize=11.5, color=MUTED)
            fig.text(0.075, y - 0.085, _short(f"{ia.smart_title(b['owner'])} · {b['role'].split(',')[0].strip()}", 40),
                     fontsize=11, color=TEXT)
            y -= 0.19
        # Grafico de la accion n.1
        top = buys[0]
        closes = _closes(top["ticker"], 180)
        if closes is not None and len(closes) > 20:
            ax = fig.add_axes([0.50, 0.15, 0.46, 0.55], facecolor=CARD)
            ax.plot(closes.index, closes.values, color=GREEN, linewidth=2.2)
            ax.fill_between(closes.index, closes.values, min(closes.values.min(), top.get("price") or 1e12) * 0.95,
                            color=GREEN, alpha=0.10)
            when = datetime.fromisoformat(top.get("trade_date") or top["filed"])
            ax.axvline(when, color=MUTED, linestyle="--", linewidth=1)
            if top.get("price"):
                ax.scatter([when], [top["price"]], s=70, color=GREEN, edgecolor=TEXT, zorder=5)
                ax.annotate(f"Insider buy ${top['price']:,.2f}", (when, top["price"]), xytext=(-12, 16),
                            textcoords="offset points", ha="right", fontsize=10.5, color=TEXT, fontweight="bold",
                            bbox={"facecolor": BG, "edgecolor": GREEN, "boxstyle": "round,pad=0.3"})
            lo = min(closes.values.min(), top.get("price") or closes.values.min())
            ax.set_ylim(lo * 0.95, max(closes.values.max(), top.get("price") or 0) * 1.04)
            for s in ("top", "right"):
                ax.spines[s].set_visible(False)
            ax.grid(color=LINE, linewidth=0.6)
            ax.tick_params(labelsize=9.5)
            import matplotlib.dates as mdates
            ax.xaxis.set_major_formatter(mdates.DateFormatter("%b"))
            fig.text(0.50, 0.73, f"${top['ticker']} · last 6 months", fontsize=13, fontweight="bold")
        return _save(plt, fig, f"daily_{day}.png")
    except Exception as e:
        print(f"   ! imagen del dia no generada: {e}")
        return None


def weekly_card(span: str, week: list[dict], by: dict, clusters: list, people: list[dict]) -> Path | None:
    try:
        plt, fig = _fig()
        _badge(fig, "WEEKLY RECAP")
        fig.text(0.04, 0.80, f"Insider buying · week of {span}", fontsize=27, fontweight="bold")
        stats = [(str(len(week)), "buys"), (ia.money(sum(b["value"] for b in week)), "bought by insiders"),
                 (str(len(by)), "stocks"), (str(len(clusters)), "cluster buys")]
        for i, (big, small) in enumerate(stats):
            x = 0.04 + [0, 0.085, 0.27, 0.36][i]
            fig.text(x, 0.66, big, fontsize=22, fontweight="bold", color=GREEN)
            fig.text(x, 0.625, small, fontsize=10, color=MUTED)
        y = 0.52
        if clusters:
            fig.text(0.04, y, "Cluster buys (2+ insiders)", fontsize=12.5, fontweight="bold", color=MUTED)
            for t, bs in clusters[:3]:
                y -= 0.065
                fig.text(0.04, y, f"${t}", fontsize=15, fontweight="bold")
                fig.text(0.46, y, f"{len({b['owner_cik'] for b in bs})} insiders · {ia.money(sum(b['value'] for b in bs))}",
                         fontsize=13, color=GREEN, ha="right")
            y -= 0.07
        if people:
            fig.text(0.04, y, "Biggest buy by an executive", fontsize=12.5, fontweight="bold", color=MUTED)
            b = people[0]
            fig.text(0.04, y - 0.065, f"${b['ticker']}", fontsize=15, fontweight="bold")
            fig.text(0.46, y - 0.065, ia.money(b["value"]), fontsize=13, color=GREEN, ha="right")
            fig.text(0.04, y - 0.105, _short(f"{ia.smart_title(b['owner'])} · {b['role'].split(',')[0].strip()}", 44),
                     fontsize=10.5, color=MUTED)
        # Barras: acciones con mas dinero comprado
        top = sorted(by.items(), key=lambda kv: -sum(b["value"] for b in kv[1]))[:7]
        if top:
            ax = fig.add_axes([0.58, 0.14, 0.38, 0.56], facecolor=BG)
            names = [f"${t}" for t, _ in top][::-1]
            vals = [sum(b["value"] for b in bs) / 1e6 for _, bs in top][::-1]
            bars = ax.barh(names, vals, color=GREEN, height=0.62)
            for bar, v in zip(bars, vals):
                ax.text(bar.get_width(), bar.get_y() + bar.get_height() / 2, f"  ${v:,.1f}M" if v >= 1 else f"  ${v * 1000:,.0f}K",
                        va="center", fontsize=10.5, color=TEXT)
            ax.set_xlim(0, max(vals) * 1.35)
            ax.tick_params(axis="y", labelsize=12, colors=TEXT)
            ax.set_xticks([])
            for s in ("top", "right", "bottom"):
                ax.spines[s].set_visible(False)
            fig.text(0.58, 0.73, "Most bought by insiders ($)", fontsize=13, fontweight="bold")
        return _save(plt, fig, f"weekly_{span.replace(' ', '_').replace('–', '-')}.png")
    except Exception as e:
        print(f"   ! imagen semanal no generada: {e}")
        return None


def _closes(ticker: str, days: int):
    try:
        import yfinance as yf
        h = yf.Ticker(ticker.replace(".", "-")).history(start=datetime.now() - timedelta(days=days))
        return h["Close"].dropna() if not h.empty else None
    except Exception:
        return None


if __name__ == "__main__":
    import insider_reddit as ir
    OUT = Path("cards_preview")
    start, end, week = ir.week_data()
    by, clusters, people, span = ir.week_summary(start, end, week)
    print(weekly_card(span, week, by, clusters, people))
    day = end.isoformat()
    buys = sorted([b for b in week if b["filed"] == day], key=lambda b: -b["value"])
    print(daily_card(day, buys))
