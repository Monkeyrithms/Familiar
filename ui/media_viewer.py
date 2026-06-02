"""
Media viewer widget — drop-in for FileViewer tabs whose file is an
image / animated image / SVG / video / audio clip.

Same surface as a QTextBrowser from the outer FileViewer's point of view:
has a `load(path)` method and is inserted as `tab["widget"]`. The outer
file-watcher calls `_reload_tab(tab)` on change, which calls back into
`load(path)` here, so live updates work the same as for text files.
"""

from __future__ import annotations

import os

from PyQt6.QtCore import Qt, QUrl, QSize, QTimer, QPointF
from PyQt6.QtGui import (
    QPixmap, QMovie, QImageReader, QPainter, QColor, QFont, QIcon,
)
from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton, QSlider,
    QScrollArea, QSizePolicy, QStackedWidget, QStyle,
)

from ui.theme import PALETTE


# ── Extension registries ────────────────────────────────────────────

IMAGE_STATIC_EXTS = {
    ".png", ".jpg", ".jpeg", ".webp", ".bmp", ".tif", ".tiff",
    ".ico", ".ppm", ".pgm", ".pbm", ".xbm", ".xpm", ".jfif",
}
# Animated raster formats — QMovie handles these (GIF + APNG + animated WebP on Qt 6+)
IMAGE_ANIMATED_EXTS = {".gif", ".apng"}
IMAGE_SVG_EXTS = {".svg", ".svgz"}
VIDEO_EXTS = {
    ".mp4", ".m4v", ".mov", ".avi", ".mkv", ".webm", ".wmv", ".flv",
    ".ogv", ".mpg", ".mpeg", ".3gp", ".3g2",
}
AUDIO_EXTS = {
    ".mp3", ".wav", ".ogg", ".oga", ".flac", ".m4a", ".aac", ".opus",
    ".wma", ".aif", ".aiff",
}

MEDIA_EXTS = (
    IMAGE_STATIC_EXTS
    | IMAGE_ANIMATED_EXTS
    | IMAGE_SVG_EXTS
    | VIDEO_EXTS
    | AUDIO_EXTS
)


def is_media_ext(ext: str) -> bool:
    return (ext or "").lower() in MEDIA_EXTS


def _kind_for_ext(ext: str) -> str:
    ext = (ext or "").lower()
    if ext in IMAGE_ANIMATED_EXTS:
        return "animated"
    if ext in IMAGE_SVG_EXTS:
        return "svg"
    if ext in IMAGE_STATIC_EXTS:
        return "image"
    if ext in VIDEO_EXTS:
        return "video"
    if ext in AUDIO_EXTS:
        return "audio"
    return ""


# ── Helpers ─────────────────────────────────────────────────────────

def _fmt_bytes(n: int) -> str:
    if n < 1024:
        return f"{n} B"
    for unit in ("KB", "MB", "GB", "TB"):
        n /= 1024.0
        if n < 1024:
            return f"{n:.1f} {unit}"
    return f"{n:.1f} PB"


def _fmt_time_ms(ms: int) -> str:
    if ms is None or ms < 0:
        ms = 0
    s, _ = divmod(int(ms), 1000)
    m, s = divmod(s, 60)
    h, m = divmod(m, 60)
    if h:
        return f"{h}:{m:02d}:{s:02d}"
    return f"{m}:{s:02d}"


# ── Image display (handles static + animated + SVG) ────────────────

class _ImagePane(QScrollArea):
    """Scrollable centered image display with fit / 100% toggle."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWidgetResizable(True)
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        p = PALETTE
        self.setStyleSheet(
            f"QScrollArea {{ background: {p['panel_alt']}; border: none; }}"
            f"QScrollBar:vertical, QScrollBar:horizontal {{ background: transparent; border: 1px solid {p['border']}; }}"
            f"QScrollBar:vertical {{ width: 14px; }} QScrollBar:horizontal {{ height: 14px; }}"
            f"QScrollBar::handle {{ background: rgba(255,255,255,0.12); border: 1px solid {p['accent_muted']}; min-height: 20px; min-width: 20px; }}"
            f"QScrollBar::add-line, QScrollBar::sub-line {{ width: 0; height: 0; }}"
        )
        self._label = QLabel()
        self._label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._label.setStyleSheet(f"background: {p['panel_alt']};")
        self.setWidget(self._label)

        self._pixmap: QPixmap | None = None
        self._movie: QMovie | None = None
        self._fit = True  # fit-to-view vs 100%

    def set_pixmap(self, pm: QPixmap):
        self._stop_movie()
        self._pixmap = pm
        self._render()

    def set_movie(self, movie: QMovie):
        self._stop_movie()
        self._pixmap = None
        self._movie = movie
        self._label.setMovie(movie)
        movie.start()

    def set_svg(self, path: str):
        self._stop_movie()
        # Render SVG to a QPixmap at the current view size, so we get
        # crisp scaling and the same fit/zoom controls as raster images.
        from PyQt6.QtSvg import QSvgRenderer
        renderer = QSvgRenderer(path)
        if not renderer.isValid():
            self._label.setText("Could not parse SVG.")
            self._pixmap = None
            return
        size = renderer.defaultSize()
        if size.isEmpty():
            size = QSize(512, 512)
        pm = QPixmap(size)
        pm.fill(Qt.GlobalColor.transparent)
        painter = QPainter(pm)
        renderer.render(painter)
        painter.end()
        self._pixmap = pm
        self._render()

    def toggle_zoom(self):
        self._fit = not self._fit
        self._render()
        return self._fit

    def is_fit(self) -> bool:
        return self._fit

    def resizeEvent(self, e):
        super().resizeEvent(e)
        if self._fit:
            self._render()

    def _stop_movie(self):
        if self._movie is not None:
            try:
                self._movie.stop()
            except Exception:
                pass
            self._label.setMovie(None)
            self._movie = None

    def _render(self):
        if self._pixmap is None or self._pixmap.isNull():
            return
        if self._fit:
            # Fit inside viewport while keeping aspect ratio.
            vp = self.viewport().size()
            vp -= QSize(4, 4)  # small inset so we don't clip against borders
            scaled = self._pixmap.scaled(
                vp,
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            )
            self._label.setPixmap(scaled)
            self._label.resize(vp)
        else:
            self._label.setPixmap(self._pixmap)
            self._label.resize(self._pixmap.size())


# ── Transport bar (video / audio) ──────────────────────────────────

class _Transport(QWidget):
    """Play/pause + seek + time + volume controls driving a QMediaPlayer."""

    def __init__(self, parent=None):
        super().__init__(parent)
        p = PALETTE
        self.setStyleSheet(
            f"QWidget {{ background: {p['panel']}; color: {p['text']}; }}"
            f"QPushButton {{ background: transparent; border: 1px solid {p['border']}; "
            f"color: {p['accent']}; padding: 2px 10px; border-radius: 3px; }}"
            f"QPushButton:hover {{ border-color: {p['accent']}; }}"
            f"QLabel {{ color: {p['muted_text']}; font-family: Consolas; font-size: 9pt; }}"
            f"QSlider::groove:horizontal {{ border: 1px solid {p['border']}; height: 4px; background: {p['panel_alt']}; }}"
            f"QSlider::handle:horizontal {{ background: {p['accent']}; width: 10px; margin: -5px 0; border-radius: 2px; }}"
            f"QSlider::sub-page:horizontal {{ background: {p['accent_muted']}; }}"
        )
        layout = QHBoxLayout(self)
        layout.setContentsMargins(8, 4, 8, 4)
        layout.setSpacing(6)

        self.play_btn = QPushButton()
        self.play_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.play_btn.setIcon(self.style().standardIcon(QStyle.StandardPixmap.SP_MediaPlay))
        self.play_btn.setFixedWidth(40)
        layout.addWidget(self.play_btn)

        self.time_label = QLabel("0:00 / 0:00")
        self.time_label.setMinimumWidth(90)
        layout.addWidget(self.time_label)

        self.seek = QSlider(Qt.Orientation.Horizontal)
        self.seek.setRange(0, 0)
        layout.addWidget(self.seek, 1)

        self.vol_label = QLabel("Vol")
        layout.addWidget(self.vol_label)
        self.vol = QSlider(Qt.Orientation.Horizontal)
        self.vol.setRange(0, 100)
        self.vol.setValue(80)
        self.vol.setFixedWidth(90)
        layout.addWidget(self.vol)


# ── Main widget ─────────────────────────────────────────────────────

class MediaViewer(QWidget):
    """Paged display for images, SVG, video, and audio files.

    Public API matches the informal 'widget' contract the FileViewer uses:
    - `load(path)` to show a file
    - read-only; no `_inhibit_reload` / `textChanged` needs
    - `stop()` to release any media-player resources (on tab close / app exit)
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        p = PALETTE
        self.setStyleSheet(f"background: {p['panel_alt']};")
        self._path = ""
        self._kind = ""

        self._root = QVBoxLayout(self)
        self._root.setContentsMargins(0, 0, 0, 0)
        self._root.setSpacing(0)

        # Info bar (top) — shows filename · dimensions/duration · size
        self._info = QLabel("")
        self._info.setStyleSheet(
            f"color: {p['muted_text']}; background: {p['panel']}; "
            f"padding: 4px 10px; font-family: Consolas; font-size: 9pt; "
            f"border-bottom: 1px solid {p['border']};"
        )
        self._info.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self._root.addWidget(self._info)

        # Body: QStackedWidget with {image pane, video widget, audio pane}
        self._stack = QStackedWidget()
        self._image_pane = _ImagePane()
        self._video_widget = None  # lazy — needs QtMultimedia
        self._audio_pane = self._build_audio_pane()
        self._stack.addWidget(self._image_pane)  # index 0
        self._stack.addWidget(self._audio_pane)  # index 1
        # Video added lazily at index 2 when first needed.
        self._root.addWidget(self._stack, 1)

        # Transport bar (shared by video + audio) — hidden for images.
        self._transport = _Transport()
        self._transport.play_btn.clicked.connect(self._toggle_play)
        self._transport.seek.sliderMoved.connect(self._on_seek_moved)
        self._transport.seek.sliderReleased.connect(self._on_seek_released)
        self._transport.vol.valueChanged.connect(self._on_vol_changed)
        self._transport.hide()
        self._root.addWidget(self._transport)

        # Zoom toggle button (only shown for images) — overlaid via the info bar
        self._zoom_btn = QPushButton("Fit")
        self._zoom_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._zoom_btn.setStyleSheet(
            f"QPushButton {{ background: transparent; border: 1px solid {p['border']}; "
            f"color: {p['accent']}; padding: 1px 8px; border-radius: 3px; font-family: Consolas; font-size: 9pt; }}"
            f"QPushButton:hover {{ border-color: {p['accent']}; }}"
        )
        self._zoom_btn.setFixedHeight(20)
        self._zoom_btn.clicked.connect(self._on_zoom_clicked)
        self._zoom_btn.hide()

        # Wrap info label + zoom button in a header row
        self._header_row = QWidget()
        hr = QHBoxLayout(self._header_row)
        hr.setContentsMargins(0, 0, 0, 0)
        hr.setSpacing(6)
        # Rebuild info bar row: info stretches, zoom button right-aligned.
        self._root.removeWidget(self._info)
        hr.addWidget(self._info, 1)
        hr.addSpacing(4)
        hr.addWidget(self._zoom_btn)
        hr.addSpacing(8)
        self._header_row.setStyleSheet(
            f"background: {p['panel']}; border-bottom: 1px solid {p['border']};"
        )
        self._root.insertWidget(0, self._header_row)

        # Media player lazy-init (shared between audio + video)
        self._player = None
        self._audio_out = None

    # ── public ──────────────────────────────────────────────────────

    def load(self, path: str):
        """Display *path*. Called on first open AND on file-watcher reload."""
        self._path = path
        ext = os.path.splitext(path)[1].lower()
        self._kind = _kind_for_ext(ext)

        # Tear down any prior media session before switching kinds.
        self._stop_media()
        self._zoom_btn.hide()
        self._transport.hide()

        if self._kind in ("image", "animated", "svg"):
            self._load_image_like(path, ext)
        elif self._kind == "video":
            self._load_video(path)
        elif self._kind == "audio":
            self._load_audio(path)
        else:
            self._info.setText(f"Unsupported media type: {ext}")

    def stop(self):
        """Release media-player resources — call from tab close / app exit."""
        self._stop_media()
        self._image_pane._stop_movie()

    # ── images / gif / svg ─────────────────────────────────────────

    def _load_image_like(self, path: str, ext: str):
        self._stack.setCurrentWidget(self._image_pane)
        self._zoom_btn.show()
        self._zoom_btn.setText("Fit" if self._image_pane.is_fit() else "100%")

        try:
            size_bytes = os.path.getsize(path)
        except OSError:
            size_bytes = 0

        if self._kind == "svg":
            self._image_pane.set_svg(path)
            self._info.setText(
                f"{os.path.basename(path)}  ·  SVG  ·  {_fmt_bytes(size_bytes)}"
            )
            return

        if self._kind == "animated":
            movie = QMovie(path)
            if not movie.isValid():
                self._info.setText(
                    f"Could not decode {os.path.basename(path)} ({ext})."
                )
                return
            # Cache frames so scrubbing / resizing stays smooth.
            movie.setCacheMode(QMovie.CacheMode.CacheAll)
            self._image_pane.set_movie(movie)
            frame_count = movie.frameCount() or 0
            size = movie.currentImage().size() if not movie.currentImage().isNull() else None
            dim = f"{size.width()}×{size.height()}" if size else "?"
            frames = f"{frame_count} frames" if frame_count else "animated"
            self._info.setText(
                f"{os.path.basename(path)}  ·  {dim}  ·  {frames}  ·  {_fmt_bytes(size_bytes)}"
            )
            return

        # Static image — QImageReader gives us dimensions without fully decoding.
        reader = QImageReader(path)
        reader.setAutoTransform(True)  # honor EXIF orientation
        img_size = reader.size()
        pm = QPixmap.fromImageReader(reader) if hasattr(QPixmap, "fromImageReader") else QPixmap(path)
        if pm.isNull():
            # Fallback via QImageReader.read()
            img = reader.read()
            if img.isNull():
                self._info.setText(
                    f"Could not decode {os.path.basename(path)} "
                    f"({reader.errorString() or ext})."
                )
                return
            pm = QPixmap.fromImage(img)

        self._image_pane.set_pixmap(pm)
        dim = f"{img_size.width()}×{img_size.height()}" if img_size.isValid() else f"{pm.width()}×{pm.height()}"
        self._info.setText(
            f"{os.path.basename(path)}  ·  {dim}  ·  {_fmt_bytes(size_bytes)}"
        )

    def _on_zoom_clicked(self):
        fit = self._image_pane.toggle_zoom()
        self._zoom_btn.setText("Fit" if fit else "100%")

    # ── video / audio ──────────────────────────────────────────────

    def _ensure_player(self):
        if self._player is not None:
            return
        from PyQt6.QtMultimedia import QMediaPlayer, QAudioOutput
        self._player = QMediaPlayer(self)
        self._audio_out = QAudioOutput(self)
        self._audio_out.setVolume(self._transport.vol.value() / 100.0)
        self._player.setAudioOutput(self._audio_out)
        self._player.positionChanged.connect(self._on_position_changed)
        self._player.durationChanged.connect(self._on_duration_changed)
        self._player.playbackStateChanged.connect(self._on_playback_state_changed)
        self._player.errorOccurred.connect(self._on_player_error)

    def _ensure_video_widget(self):
        if self._video_widget is not None:
            return
        from PyQt6.QtMultimediaWidgets import QVideoWidget
        self._video_widget = QVideoWidget()
        p = PALETTE
        self._video_widget.setStyleSheet(f"background: #000;")
        self._stack.addWidget(self._video_widget)

    def _load_video(self, path: str):
        self._ensure_player()
        self._ensure_video_widget()
        self._player.setVideoOutput(self._video_widget)
        self._player.setSource(QUrl.fromLocalFile(path))
        self._stack.setCurrentWidget(self._video_widget)
        self._transport.show()
        try:
            size_bytes = os.path.getsize(path)
        except OSError:
            size_bytes = 0
        self._info.setText(
            f"{os.path.basename(path)}  ·  video  ·  {_fmt_bytes(size_bytes)}"
        )

    def _load_audio(self, path: str):
        self._ensure_player()
        # Detach any video sink from a prior video session.
        try:
            self._player.setVideoOutput(None)
        except Exception:
            pass
        self._player.setSource(QUrl.fromLocalFile(path))
        self._stack.setCurrentWidget(self._audio_pane)
        self._audio_title.setText(os.path.basename(path))
        self._transport.show()
        try:
            size_bytes = os.path.getsize(path)
        except OSError:
            size_bytes = 0
        self._info.setText(
            f"{os.path.basename(path)}  ·  audio  ·  {_fmt_bytes(size_bytes)}"
        )

    def _build_audio_pane(self) -> QWidget:
        p = PALETTE
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.setContentsMargins(24, 24, 24, 24)
        lay.setSpacing(12)
        lay.addStretch(1)

        icon_lbl = QLabel("\u266B")  # musical notes
        icon_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        icon_lbl.setStyleSheet(
            f"color: {p['accent']}; font-size: 72pt; background: transparent;"
        )
        lay.addWidget(icon_lbl)

        self._audio_title = QLabel("")
        self._audio_title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._audio_title.setWordWrap(True)
        self._audio_title.setStyleSheet(
            f"color: {p['text']}; font-family: Consolas; font-size: 11pt; background: transparent;"
        )
        lay.addWidget(self._audio_title)

        lay.addStretch(2)
        return w

    # ── transport plumbing ─────────────────────────────────────────

    def _toggle_play(self):
        if self._player is None:
            return
        from PyQt6.QtMultimedia import QMediaPlayer
        state = self._player.playbackState()
        if state == QMediaPlayer.PlaybackState.PlayingState:
            self._player.pause()
        else:
            self._player.play()

    def _on_seek_moved(self, value: int):
        self._transport.time_label.setText(
            f"{_fmt_time_ms(value)} / {_fmt_time_ms(self._player.duration() if self._player else 0)}"
        )

    def _on_seek_released(self):
        if self._player is None:
            return
        self._player.setPosition(self._transport.seek.value())

    def _on_vol_changed(self, value: int):
        if self._audio_out is not None:
            self._audio_out.setVolume(value / 100.0)

    def _on_position_changed(self, pos: int):
        if not self._transport.seek.isSliderDown():
            self._transport.seek.setValue(pos)
        self._transport.time_label.setText(
            f"{_fmt_time_ms(pos)} / {_fmt_time_ms(self._player.duration() if self._player else 0)}"
        )

    def _on_duration_changed(self, dur: int):
        self._transport.seek.setRange(0, max(0, dur))
        self._transport.time_label.setText(
            f"{_fmt_time_ms(self._player.position() if self._player else 0)} / {_fmt_time_ms(dur)}"
        )

    def _on_playback_state_changed(self, state):
        from PyQt6.QtMultimedia import QMediaPlayer
        playing = state == QMediaPlayer.PlaybackState.PlayingState
        self._transport.play_btn.setIcon(
            self.style().standardIcon(
                QStyle.StandardPixmap.SP_MediaPause if playing else QStyle.StandardPixmap.SP_MediaPlay
            )
        )

    def _on_player_error(self, err, msg: str = ""):
        if err:
            base = os.path.basename(self._path) if self._path else "file"
            self._info.setText(f"Playback error on {base}: {msg or err}")

    def _stop_media(self):
        if self._player is not None:
            try:
                self._player.stop()
                self._player.setSource(QUrl())
            except Exception:
                pass
