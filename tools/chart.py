"""
Chart generation tool — vispy-based rendering for line, bar, scatter,
candlestick, and heatmap charts with a dark theme matching the Agent UI.

Renders to PNG via vispy SceneCanvas + PIL, emits a Qt signal so the chat
widget can display an inline ChartCard.

vispy_dashboard is used for OHLCV finance dashboards when available.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import numpy as np
from PyQt6.QtCore import QObject, Qt, pyqtSignal

# vispy.scene costs ~540ms to import — far too much to pay at startup for a tool
# that's only used when the agent actually draws a chart. It's loaded lazily by
# _ensure_vispy() on first chart render. `scene` / `ColorArray` are bound as
# module globals there so the existing render code keeps referring to them
# unchanged.
scene = None  # type: ignore[assignment]
ColorArray = None  # type: ignore[assignment]
_vispy_ready = False


def _ensure_vispy() -> None:
    """Import vispy.scene on first use (keeps the ~540ms cost off startup)."""
    global scene, ColorArray, _vispy_ready
    if _vispy_ready:
        return
    import vispy
    vispy.use("pyqt6")
    from vispy import scene as _scene
    from vispy.color import ColorArray as _ColorArray
    scene = _scene
    ColorArray = _ColorArray
    _vispy_ready = True

# ---------------------------------------------------------------------------
# Qt signal bridge
# ---------------------------------------------------------------------------

class ChartBridge(QObject):
    """Fires chart_ready(path, title, chart_type) from any thread."""
    chart_ready = pyqtSignal(str, str, str)


_chart_bridge = ChartBridge()

# ---------------------------------------------------------------------------
# Theme constants
# ---------------------------------------------------------------------------

_BG        = "#111214"
_PANEL     = "#18191c"
_TEXT      = "#e0e0e0"
_MUTED     = "#6e7070"
_ACCENT    = "#00c8a0"
_ACCENT2   = "#4e9fd4"
_ACCENT3   = "#d4844e"
_GRID      = "#2a2a2e"
_GREEN     = "#26a69a"
_RED       = "#ef5350"
_WATERMARK = "#2e2e32"

_SERIES_COLORS = [_ACCENT, _ACCENT2, _ACCENT3,
                  "#b48ead", "#ebcb8b", "#88c0d0", "#a3be8c"]

# Layout constants (pixels)
_MARGIN         = 10   # outer grid margin
_YAXIS_W        = 60   # y-axis widget max width
_XAXIS_H        = 42   # x-axis widget max height
_TITLE_H        = 30   # title row height when title present
_LABEL_FONT     = 9    # axis label font size (PIL points)

# ---------------------------------------------------------------------------
# Output directory
# ---------------------------------------------------------------------------

_CHARTS_DIR = Path(__file__).parent.parent / "data" / "charts"


def _ensure_dir() -> Path:
    _CHARTS_DIR.mkdir(parents=True, exist_ok=True)
    return _CHARTS_DIR


def _unique_path(title: str, chart_type: str) -> str:
    out_dir = _ensure_dir()
    safe = "".join(c if c.isalnum() or c in "_- " else "_" for c in title).strip().replace(" ", "_")
    if not safe:
        safe = chart_type
    base = out_dir / f"{chart_type}_{safe}"
    candidate = f"{base}.png"
    counter = 1
    while os.path.exists(candidate):
        candidate = f"{base}_{counter}.png"
        counter += 1
    return candidate

# ---------------------------------------------------------------------------
# PIL helpers — title, watermark, bar/candlestick x labels
# ---------------------------------------------------------------------------

def _hex_to_rgb(hex_color: str) -> tuple[int, int, int]:
    h = hex_color.lstrip("#")
    return tuple(int(h[i:i+2], 16) for i in (0, 2, 4))


def _pil_overlay(img_arr: np.ndarray, title: str, watermark: bool = True,
                 x_labels: list[str] | None = None,
                 canvas_w: int = 1200, canvas_h: int = 540,
                 has_title_row: bool = False) -> np.ndarray:
    """Overlay title, watermark, and optional string x-labels on rendered image."""
    from PIL import Image, ImageDraw, ImageFont

    img = Image.fromarray(img_arr)
    draw = ImageDraw.Draw(img)

    # Attempt a nice font; fall back to PIL default
    font_title = ImageFont.load_default()
    font_label = ImageFont.load_default()
    try:
        import platform
        if platform.system() == "Windows":
            candidates = ["C:/Windows/Fonts/segoeui.ttf",
                          "C:/Windows/Fonts/arial.ttf",
                          "C:/Windows/Fonts/tahoma.ttf"]
        else:
            candidates = ["/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
                          "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf"]
        for fp in candidates:
            if os.path.exists(fp):
                font_title = ImageFont.truetype(fp, 14)
                font_label = ImageFont.truetype(fp, 9)
                break
    except Exception:
        pass

    text_rgb  = _hex_to_rgb(_TEXT)
    muted_rgb = _hex_to_rgb(_MUTED)
    wm_rgb    = _hex_to_rgb(_WATERMARK)

    # Title
    if title:
        y_pos = 8 if has_title_row else 8
        draw.text((canvas_w // 2, y_pos), title,
                  fill=text_rgb, font=font_title, anchor="mt")

    # Watermark
    if watermark:
        draw.text((canvas_w - 8, canvas_h - 6), "Agent",
                  fill=wm_rgb, font=font_label, anchor="rs")

    # String x-labels (bar chart, candlestick)
    if x_labels:
        n = len(x_labels)
        # Approximate plot area x bounds
        plot_x0 = _MARGIN + _YAXIS_W
        plot_x1 = canvas_w - _MARGIN - 5
        plot_w  = plot_x1 - plot_x0
        # y position: just below plot area
        plot_y1 = canvas_h - _MARGIN - _XAXIS_H + 2
        for i, lbl in enumerate(x_labels):
            px = int(plot_x0 + (i + 0.5) / n * plot_w)
            draw.text((px, plot_y1), str(lbl),
                      fill=muted_rgb, font=font_label, anchor="mt")

    return np.array(img)


# ---------------------------------------------------------------------------
# Canvas + grid factory
# ---------------------------------------------------------------------------

def _make_canvas_grid(w: int = 1200, h: int = 540, has_title: bool = False):
    """Create SceneCanvas + grid layout; return (canvas, view, grid, row_data)."""
    canvas = scene.SceneCanvas(size=(w, h), bgcolor=_BG, show=False)
    grid   = canvas.central_widget.add_grid(margin=_MARGIN)

    data_row = 0
    if has_title:
        title_view = grid.add_view(row=0, col=0, col_span=2, bgcolor=_BG)
        title_view.height_min = _TITLE_H
        title_view.height_max = _TITLE_H
        data_row = 1

    # Y-axis widget
    y_axis = scene.AxisWidget(
        orientation="left",
        axis_color=_MUTED,
        tick_color=_MUTED,
        text_color=_TEXT,
        axis_font_size=8,
        tick_font_size=8,
    )
    y_wdg = grid.add_widget(y_axis, row=data_row, col=0)
    y_wdg.width_max = _YAXIS_W

    # Plot view
    view = grid.add_view(row=data_row, col=1, bgcolor=_PANEL, border_color=_GRID)

    # X-axis widget
    x_axis = scene.AxisWidget(
        orientation="bottom",
        axis_color=_MUTED,
        tick_color=_MUTED,
        text_color=_TEXT,
        axis_font_size=8,
        tick_font_size=8,
    )
    x_wdg = grid.add_widget(x_axis, row=data_row + 1, col=1)
    x_wdg.height_max = _XAXIS_H

    x_axis.link_view(view)
    y_axis.link_view(view)
    view.camera = "panzoom"

    return canvas, view


def _add_grid_lines(view):
    scene.visuals.GridLines(color=_GRID, parent=view.scene)


def _render_save(canvas, img_arr, title, chart_type,
                 x_labels=None, canvas_w=1200, canvas_h=540) -> str:
    """PIL-overlay then save; return path."""
    img_arr = _pil_overlay(
        img_arr, title, watermark=True,
        x_labels=x_labels,
        canvas_w=canvas_w, canvas_h=canvas_h,
        has_title_row=bool(title),
    )
    path = _unique_path(title, chart_type)
    from PIL import Image
    Image.fromarray(img_arr).save(path)
    return path


# ---------------------------------------------------------------------------
# Individual chart renderers
# ---------------------------------------------------------------------------

def _render_line(data: dict, title: str, x_label: str, y_label: str) -> str:
    W, H = 1200, 540
    canvas, view = _make_canvas_grid(W, H, has_title=bool(title))
    _add_grid_lines(view)

    x_min = x_max = y_min = y_max = None

    def _track(x_arr, y_arr):
        nonlocal x_min, x_max, y_min, y_max
        x_min = float(np.min(x_arr)) if x_min is None else min(x_min, float(np.min(x_arr)))
        x_max = float(np.max(x_arr)) if x_max is None else max(x_max, float(np.max(x_arr)))
        y_min = float(np.min(y_arr)) if y_min is None else min(y_min, float(np.min(y_arr)))
        y_max = float(np.max(y_arr)) if y_max is None else max(y_max, float(np.max(y_arr)))

    if "series" in data:
        for idx, series in enumerate(data["series"]):
            color = _SERIES_COLORS[idx % len(_SERIES_COLORS)]
            x = np.asarray(series["x"], dtype=float)
            y = np.asarray(series["y"], dtype=float)
            _track(x, y)
            pos = np.column_stack([x, y]).astype(np.float32)
            scene.visuals.Line(pos=pos, color=color, width=2,
                               antialias=True, parent=view.scene)
    else:
        x = np.asarray(data["x"], dtype=float)
        y = np.asarray(data["y"], dtype=float)
        _track(x, y)
        pos = np.column_stack([x, y]).astype(np.float32)
        scene.visuals.Line(pos=pos, color=_ACCENT, width=2,
                           antialias=True, parent=view.scene)

    if x_min is not None and x_max is not None:
        pad_x = (x_max - x_min) * 0.03 or 0.5
        pad_y = (y_max - y_min) * 0.05 or 0.5
        view.camera.set_range(
            x=(x_min - pad_x, x_max + pad_x),
            y=(y_min - pad_y, y_max + pad_y),
        )

    img = canvas.render()
    return _render_save(canvas, img, title, "line", canvas_w=W, canvas_h=H)


def _render_bar(data: dict, title: str, x_label: str, y_label: str) -> str:
    W, H = 1200, 540
    canvas, view = _make_canvas_grid(W, H, has_title=bool(title))
    _add_grid_lines(view)

    if "series" in data:
        x_labels_list = data.get("x_labels", [])
        series_list   = data["series"]
        n_groups = max(len(s["values"]) for s in series_list)
        n_series = len(series_list)
        bar_w = 0.8 / max(n_series, 1)
        all_vals = [v for s in series_list for v in s["values"]]
        y_max = max(all_vals) if all_vals else 1
        y_min = min(0.0, min(all_vals) if all_vals else 0)

        verts_all, faces_all, colors_all = [], [], []
        face_offset = 0
        for si, series in enumerate(series_list):
            color_hex = _SERIES_COLORS[si % len(_SERIES_COLORS)]
            rgba = [c / 255 for c in _hex_to_rgb(color_hex)] + [0.85]
            offset = (si - (n_series - 1) / 2.0) * bar_w
            for gi, val in enumerate(series["values"]):
                x0 = gi + offset - bar_w * 0.45
                x1 = gi + offset + bar_w * 0.45
                y0, y1 = 0.0, float(val)
                verts_all += [[x0, y0], [x1, y0], [x1, y1], [x0, y1]]
                faces_all += [[face_offset, face_offset+1, face_offset+2],
                              [face_offset, face_offset+2, face_offset+3]]
                colors_all += [rgba] * 4
                face_offset += 4
        if not x_labels_list:
            x_labels_list = [str(i) for i in range(n_groups)]
    else:
        labels  = data["labels"]
        values  = data["values"]
        n_groups = len(labels)
        n_series = 1
        bar_w = 0.7
        y_max = max(values) if values else 1
        y_min = min(0.0, min(values) if values else 0)
        x_labels_list = labels

        verts_all, faces_all, colors_all = [], [], []
        face_offset = 0
        for i, val in enumerate(values):
            color_hex = _SERIES_COLORS[i % len(_SERIES_COLORS)]
            rgba = [c / 255 for c in _hex_to_rgb(color_hex)] + [0.85]
            x0, x1 = i - bar_w / 2, i + bar_w / 2
            y0, y1 = 0.0, float(val)
            verts_all += [[x0, y0], [x1, y0], [x1, y1], [x0, y1]]
            faces_all += [[face_offset, face_offset+1, face_offset+2],
                          [face_offset, face_offset+2, face_offset+3]]
            colors_all += [rgba] * 4
            face_offset += 4

    if verts_all:
        mesh = scene.visuals.Mesh(
            vertices=np.array(verts_all, dtype=np.float32),
            faces=np.array(faces_all, dtype=np.uint32),
            vertex_colors=np.array(colors_all, dtype=np.float32),
            parent=view.scene,
        )

    pad_y = (y_max - y_min) * 0.06 or 0.5
    view.camera.set_range(
        x=(-0.5, n_groups - 0.5),
        y=(y_min - pad_y * 0.5, y_max + pad_y),
    )

    img = canvas.render()
    return _render_save(canvas, img, title, "bar",
                        x_labels=x_labels_list, canvas_w=W, canvas_h=H)


def _render_scatter(data: dict, title: str, x_label: str, y_label: str) -> str:
    W, H = 800, 700
    canvas, view = _make_canvas_grid(W, H, has_title=bool(title))
    _add_grid_lines(view)

    x = np.asarray(data["x"], dtype=float)
    y = np.asarray(data["y"], dtype=float)
    pos = np.column_stack([x, y]).astype(np.float32)

    raw_colors = data.get("color", _ACCENT)
    sizes = data.get("size", 8)

    if isinstance(raw_colors, list):
        if all(isinstance(c, (int, float)) for c in raw_colors):
            # Scalar → map through a cool-to-warm colormap
            arr = np.asarray(raw_colors, dtype=float)
            arr_n = (arr - arr.min()) / (arr.ptp() or 1.0)
            face_colors = np.column_stack([
                arr_n,
                1 - arr_n,
                0.6 * np.ones(len(arr_n)),
                np.ones(len(arr_n)),
            ]).astype(np.float32)
        else:
            face_colors = raw_colors
    else:
        face_colors = raw_colors

    markers = scene.visuals.Markers(parent=view.scene)
    markers.set_data(
        pos,
        face_color=face_colors,
        size=sizes,
        edge_color=_BG,
        edge_width=0.5,
    )

    pad_x = (float(x.max()) - float(x.min())) * 0.05 or 0.5
    pad_y = (float(y.max()) - float(y.min())) * 0.05 or 0.5
    view.camera.set_range(
        x=(float(x.min()) - pad_x, float(x.max()) + pad_x),
        y=(float(y.min()) - pad_y, float(y.max()) + pad_y),
    )

    img = canvas.render()
    return _render_save(canvas, img, title, "scatter", canvas_w=W, canvas_h=H)


def _render_candlestick(data: dict, title: str, x_label: str, y_label: str) -> str:
    W, H = 1200, 540
    has_volume = bool(data.get("volume"))

    if has_volume:
        # Two-panel canvas: price on top (row=1), volume below (row=2)
        canvas = scene.SceneCanvas(size=(W, H), bgcolor=_BG, show=False)
        grid   = canvas.central_widget.add_grid(margin=_MARGIN)

        data_row = 0
        if title:
            tv = grid.add_view(row=0, col=0, col_span=2, bgcolor=_BG)
            tv.height_min = _TITLE_H
            tv.height_max = _TITLE_H
            data_row = 1

        # Y-axis (price)
        ya_price = scene.AxisWidget(orientation="left", axis_color=_MUTED,
                                    tick_color=_MUTED, text_color=_TEXT,
                                    axis_font_size=8, tick_font_size=8)
        grid.add_widget(ya_price, row=data_row, col=0).width_max = _YAXIS_W

        price_view = grid.add_view(
            row=data_row, col=1, bgcolor=_PANEL, border_color=_GRID)
        price_view.stretch = (1, 3)

        # Volume view
        ya_vol = scene.AxisWidget(orientation="left", axis_color=_MUTED,
                                  tick_color=_MUTED, text_color=_TEXT,
                                  axis_font_size=7, tick_font_size=7)
        grid.add_widget(ya_vol, row=data_row + 1, col=0).width_max = _YAXIS_W

        vol_view = grid.add_view(
            row=data_row + 1, col=1, bgcolor=_PANEL, border_color=_GRID)
        vol_view.stretch = (1, 1)

        xa_vol = scene.AxisWidget(orientation="bottom", axis_color=_MUTED,
                                  tick_color=_MUTED, text_color=_TEXT,
                                  axis_font_size=8, tick_font_size=8)
        grid.add_widget(xa_vol, row=data_row + 2, col=1).height_max = _XAXIS_H

        xa_vol.link_view(vol_view)
        ya_price.link_view(price_view)
        ya_vol.link_view(vol_view)
        price_view.camera = "panzoom"
        vol_view.camera   = "panzoom"

        main_view = price_view
    else:
        canvas, main_view = _make_canvas_grid(W, H, has_title=bool(title))

    _add_grid_lines(main_view)

    dates  = data["dates"]
    opens  = np.asarray(data["open"],  dtype=float)
    highs  = np.asarray(data["high"],  dtype=float)
    lows   = np.asarray(data["low"],   dtype=float)
    closes = np.asarray(data["close"], dtype=float)
    n      = len(dates)

    # Build wick line segments: pairs of (x, low) → (x, high)
    wick_pos  = []
    wick_conn = []
    seg_start = 0
    for i in range(n):
        wick_pos.append([i, lows[i]])
        wick_pos.append([i, highs[i]])
        wick_conn.append(True)   # connect
        wick_conn.append(False)  # break
        seg_start += 2

    wick_colors = []
    for i in range(n):
        c = _GREEN if closes[i] >= opens[i] else _RED
        rgb = [v / 255 for v in _hex_to_rgb(c)] + [1.0]
        wick_colors += [rgb, rgb]

    if wick_pos:
        scene.visuals.Line(
            pos=np.array(wick_pos, dtype=np.float32),
            connect=np.array(wick_conn, dtype=bool),
            color=np.array(wick_colors, dtype=np.float32),
            width=1.0,
            parent=main_view.scene,
        )

    # Build body rectangles as mesh
    verts, faces, colors = [], [], []
    face_off = 0
    bw = 0.4
    for i in range(n):
        o, c_close = float(opens[i]), float(closes[i])
        y0, y1 = (o, c_close) if c_close >= o else (c_close, o)
        if y1 - y0 < (highs[i] - lows[i]) * 0.005:
            y1 = y0 + (highs[i] - lows[i]) * 0.005 + 1e-6
        color_hex = _GREEN if c_close >= o else _RED
        rgba = [v / 255 for v in _hex_to_rgb(color_hex)] + [0.9]
        x0, x1 = i - bw, i + bw
        verts += [[x0, y0], [x1, y0], [x1, y1], [x0, y1]]
        faces += [[face_off, face_off+1, face_off+2],
                  [face_off, face_off+2, face_off+3]]
        colors += [rgba] * 4
        face_off += 4

    if verts:
        scene.visuals.Mesh(
            vertices=np.array(verts, dtype=np.float32),
            faces=np.array(faces, dtype=np.uint32),
            vertex_colors=np.array(colors, dtype=np.float32),
            parent=main_view.scene,
        )

    price_pad = (float(highs.max()) - float(lows.min())) * 0.03 or 0.5
    main_view.camera.set_range(
        x=(-0.5, n - 0.5),
        y=(float(lows.min()) - price_pad, float(highs.max()) + price_pad),
    )

    if has_volume:
        vols = np.asarray(data["volume"], dtype=float)
        vol_verts, vol_faces, vol_colors = [], [], []
        vf_off = 0
        for i in range(n):
            c_hex = _GREEN if closes[i] >= opens[i] else _RED
            rgba  = [v / 255 for v in _hex_to_rgb(c_hex)] + [0.6]
            x0, x1 = i - 0.4, i + 0.4
            vol_verts += [[x0, 0.0], [x1, 0.0], [x1, float(vols[i])], [x0, float(vols[i])]]
            vol_faces  += [[vf_off, vf_off+1, vf_off+2], [vf_off, vf_off+2, vf_off+3]]
            vol_colors += [rgba] * 4
            vf_off += 4
        if vol_verts:
            scene.visuals.Mesh(
                vertices=np.array(vol_verts, dtype=np.float32),
                faces=np.array(vol_faces,   dtype=np.uint32),
                vertex_colors=np.array(vol_colors, dtype=np.float32),
                parent=vol_view.scene,
            )
        vol_pad = float(vols.max()) * 0.05 or 1.0
        vol_view.camera.set_range(x=(-0.5, n - 0.5), y=(0, float(vols.max()) + vol_pad))

    # Build date string labels for x axis (every ~10 bars)
    x_labels_map: list[str] = []
    step = max(1, n // 10)
    for i in range(n):
        if i % step == 0:
            d = dates[i]
            try:
                if isinstance(d, (int, float)):
                    from datetime import datetime
                    x_labels_map.append(datetime.fromtimestamp(d).strftime("%m/%d"))
                else:
                    x_labels_map.append(str(d)[:10])
            except Exception:
                x_labels_map.append(str(d))
        else:
            x_labels_map.append("")

    img = canvas.render()

    # PIL: title + watermark + date labels along bottom
    visible_labels = [x_labels_map[i] for i in range(0, n, step)]
    return _render_save(canvas, img, title, "candlestick",
                        x_labels=None, canvas_w=W, canvas_h=H)


def _render_heatmap(data: dict, title: str, x_label: str, y_label: str) -> str:
    W, H = 800, 700
    matrix     = np.array(data["matrix"], dtype=float)
    row_labels = data.get("row_labels", [str(i) for i in range(matrix.shape[0])])
    col_labels = data.get("col_labels", [str(j) for j in range(matrix.shape[1])])

    canvas = scene.SceneCanvas(size=(W, H), bgcolor=_BG, show=False)
    grid   = canvas.central_widget.add_grid(margin=_MARGIN)

    data_row = 0
    if title:
        tv = grid.add_view(row=0, col=0, col_span=2, bgcolor=_BG)
        tv.height_min = _TITLE_H
        tv.height_max = _TITLE_H
        data_row = 1

    view = grid.add_view(row=data_row, col=1, bgcolor=_PANEL, border_color=_GRID)
    view.camera = "panzoom"

    rows, cols = matrix.shape

    # Normalize to 0-1 for colormap
    vmin, vmax = matrix.min(), matrix.max()
    norm = (matrix - vmin) / (vmax - vmin + 1e-12)

    # Apply a custom teal-to-amber colormap
    # Each pixel: image needs shape (rows, cols, 4) RGBA float32
    r = norm * 0.84 + (1 - norm) * 0.04
    g = norm * 0.65 + (1 - norm) * 0.79
    b = norm * 0.00 + (1 - norm) * 0.80
    rgba_img = np.stack([r, g, b, np.ones_like(r)], axis=-1).astype(np.float32)

    img_vis = scene.visuals.Image(
        rgba_img,
        texture_format="rgba32f",
        parent=view.scene,
    )

    view.camera.set_range(x=(0, cols), y=(0, rows))

    img_arr = canvas.render()

    # PIL overlay: title + col/row labels
    from PIL import Image as PILImage, ImageDraw, ImageFont
    pil = PILImage.fromarray(img_arr)
    draw = ImageDraw.Draw(pil)

    font_sm = ImageFont.load_default()
    try:
        import platform
        fps = (["C:/Windows/Fonts/arial.ttf"] if platform.system() == "Windows"
               else ["/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"])
        for fp in fps:
            if os.path.exists(fp):
                font_sm = ImageFont.truetype(fp, 8)
                break
    except Exception:
        pass

    muted_rgb = _hex_to_rgb(_MUTED)
    text_rgb  = _hex_to_rgb(_TEXT)
    wm_rgb    = _hex_to_rgb(_WATERMARK)

    # Approximate plot area
    plot_x0 = _MARGIN + 5
    plot_x1 = W - _MARGIN - 5
    plot_y0 = (_TITLE_H + _MARGIN) if title else _MARGIN
    plot_y1 = H - _MARGIN - 5

    pw = plot_x1 - plot_x0
    ph = plot_y1 - plot_y0
    cell_w = pw / cols
    cell_h = ph / rows

    # Col labels across top
    for j, lbl in enumerate(col_labels):
        px = int(plot_x0 + (j + 0.5) * cell_w)
        draw.text((px, plot_y0 + 2), str(lbl)[:12],
                  fill=muted_rgb, font=font_sm, anchor="mt")

    # Row labels on left
    for i, lbl in enumerate(row_labels):
        py = int(plot_y0 + (rows - i - 0.5) * cell_h)
        draw.text((plot_x0 - 2, py), str(lbl)[:10],
                  fill=muted_rgb, font=font_sm, anchor="rs")

    # Cell annotations if small enough
    if rows * cols <= 200:
        for i in range(rows):
            for j in range(cols):
                val = matrix[i, j]
                px = int(plot_x0 + (j + 0.5) * cell_w)
                py = int(plot_y0 + (rows - i - 0.5) * cell_h)
                lum = 0.299 * rgba_img[i, j, 0] + 0.587 * rgba_img[i, j, 1] + 0.114 * rgba_img[i, j, 2]
                fc  = (30, 30, 30) if lum > 0.5 else (230, 230, 230)
                draw.text((px, py), f"{val:.2g}", fill=fc, font=font_sm, anchor="mm")

    if title:
        draw.text((W // 2, 8), title, fill=text_rgb, font=font_sm, anchor="mt")
    draw.text((W - 6, H - 5), "Agent", fill=wm_rgb, font=font_sm, anchor="rs")

    path = _unique_path(title, "heatmap")
    pil.save(path)
    return path


# ---------------------------------------------------------------------------
# GUI-thread dispatch
# ---------------------------------------------------------------------------
# vispy requires its SceneCanvas (which creates a QWindow-based offscreen
# surface) to be constructed on the Qt GUI thread. Agent tool calls arrive
# on worker threads. QTimer.singleShot() from a worker thread fires in THAT
# thread's event loop (which doesn't exist) — the callback never runs.
#
# The correct Qt pattern: a QObject living in the main thread with a
# QueuedConnection signal. Emitting from any thread posts to the owner's
# thread event loop, which IS the main Qt event loop. We pair this with a
# threading.Event to block the worker until the render finishes.

class _GuiInvoker(QObject):
    """Routes callables to the GUI thread via a queued Qt signal.

    Must be instantiated from the main thread (module import time).
    """
    _sig = pyqtSignal(object)   # carries a plain Python callable

    def __init__(self):
        super().__init__()
        # QueuedConnection: slot always executes in this object's (main) thread
        self._sig.connect(self._execute, Qt.ConnectionType.QueuedConnection)

    @staticmethod
    def _execute(fn):
        fn()

    def invoke(self, fn):
        """Emit from any thread; fn() will run on the GUI thread."""
        self._sig.emit(fn)


# Created at import time — module is loaded on the main thread during app
# startup, so this object automatically lives in the main thread.
_gui_invoker = _GuiInvoker()


def _call_on_gui_thread(fn, *args, timeout: float = 60.0):
    """Run fn(*args) on the Qt GUI thread; block the caller until done."""
    import threading
    from PyQt6.QtCore import QThread
    from PyQt6.QtWidgets import QApplication

    app = QApplication.instance()

    # Already on the GUI thread — call directly.
    if app is None or QThread.currentThread() is app.thread():
        return fn(*args)

    result: list = [None, None]   # [value, exception]
    done = threading.Event()

    def _wrapper():
        try:
            result[0] = fn(*args)
        except Exception as exc:
            result[1] = exc
        finally:
            done.set()

    _gui_invoker.invoke(_wrapper)
    if not done.wait(timeout):
        raise TimeoutError(f"Chart render did not complete within {timeout}s")
    if result[1] is not None:
        raise result[1]
    return result[0]


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

_RENDERERS = {
    "line":        _render_line,
    "bar":         _render_bar,
    "scatter":     _render_scatter,
    "candlestick": _render_candlestick,
    "heatmap":     _render_heatmap,
}


def chart(
    type: str,
    data: dict,
    title: str = "",
    x_label: str = "",
    y_label: str = "",
) -> str:
    """
    Render a chart and save it to data/charts/.

    Supported types: line, bar, scatter, candlestick, heatmap

    Data schemas:
      line:        {"x": [...], "y": [...], "label": ""}
                   OR {"series": [{"x": [...], "y": [...], "label": "..."}, ...]}
      bar:         {"labels": [...], "values": [...]}
                   OR {"series": [{"label": "...", "values": [...]}], "x_labels": [...]}
      scatter:     {"x": [...], "y": [...], "labels": [...opt],
                    "color": [...opt floats or css string],
                    "size": [...opt or scalar]}
      candlestick: {"dates": [...iso or timestamps], "open": [...], "high": [...],
                    "low": [...], "close": [...], "volume": [...opt]}
      heatmap:     {"matrix": [[...]], "row_labels": [...], "col_labels": [...]}
    """
    _ensure_vispy()  # load vispy.scene now (deferred off startup)
    chart_type = type.strip().lower()
    if chart_type not in _RENDERERS:
        return json.dumps({
            "error": f"Unknown chart type '{chart_type}'. Supported: {list(_RENDERERS)}"
        })

    try:
        path = _call_on_gui_thread(
            _RENDERERS[chart_type], data, title, x_label, y_label
        )
    except Exception as exc:
        import traceback
        return json.dumps({"error": f"Chart render failed: {exc}",
                           "detail": traceback.format_exc()})

    # Emit Qt signal (safe from any thread via Qt queued connection)
    try:
        _chart_bridge.chart_ready.emit(path, title or chart_type, chart_type)
    except Exception:
        pass

    return json.dumps({
        "chart_path": path,
        "title": title,
        "type": chart_type,
        "note": "Chart saved. Inline card shown in chat.",
    })


def get_chart_bridge() -> ChartBridge:
    """Return the module-level bridge (used by chat_widget to connect signal)."""
    return _chart_bridge


# ---------------------------------------------------------------------------
# Tool registration
# ---------------------------------------------------------------------------

def _register():
    try:
        from tools.registry import registry
        registry.register(
            name="chart",
            description=(
                "Render chart (GPU). Saved to data/charts/.\n"
                "Types: line|bar|scatter|candlestick|heatmap.\n"
                "line:{x,y,label}|{series:[{x,y,label}]} "
                "bar:{labels,values}|{series,x_labels} "
                "scatter:{x,y,color?,size?} "
                "candlestick:{dates,open,high,low,close,volume?} "
                "heatmap:{matrix,row_labels,col_labels}"
            ),
            parameters={
                "type": "object",
                "properties": {
                    "type": {
                        "type": "string",
                        "enum": ["line", "bar", "scatter", "candlestick", "heatmap"],
                        "description": "Chart type.",
                    },
                    "data": {
                        "type": "object",
                        "description": "Chart data (schema depends on type, see description).",
                    },
                    "title": {"type": "string", "description": "Chart title.", "default": ""},
                    "x_label": {"type": "string", "description": "X-axis label.", "default": ""},
                    "y_label": {"type": "string", "description": "Y-axis label.", "default": ""},
                },
                "required": ["type", "data"],
            },
            execute=chart,
        )
    except Exception:
        pass


_register()
