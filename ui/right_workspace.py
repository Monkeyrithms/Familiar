"""
Right splitter workspace: Notes, Calendar, Browser, File (viewer + tree), Terminal (integrated shells).
"""

from __future__ import annotations

from pathlib import Path

from PyQt6.QtWidgets import (
    QWidget,
    QVBoxLayout,
    QHBoxLayout,
    QPushButton,
    QFrame,
    QStackedWidget,
    QLineEdit,
    QSizePolicy,
    QLabel,
    QButtonGroup,
    QDialog,
    QApplication,
)
from PyQt6.QtCore import Qt, QUrl, QTimer, QSize
from PyQt6.QtGui import QFont, QColor

from ui.theme import PALETTE
from ui.themed_tab_widget import ThemedClosableTabWidget
from ui.file_viewer import FileViewer
from ui.terminal_workspace import TerminalWorkspacePanel, MultiConvTerminalPanel
from ui.workspace_notes_calendar import NotesWorkspacePanel, CalendarWorkspacePanel

try:
    from PyQt6.QtWebEngineWidgets import QWebEngineView
    from PyQt6.QtWebEngineCore import (
        QWebEngineSettings,
        QWebEnginePage,
        QWebEngineProfile,
        QWebEngineScript,
    )

    _WEBENGINE_AVAILABLE = True
except ImportError:
    QWebEngineView = None  # type: ignore[misc, assignment]
    QWebEngineSettings = None  # type: ignore[misc, assignment]
    QWebEnginePage = None  # type: ignore[misc, assignment]
    QWebEngineProfile = None  # type: ignore[misc, assignment]
    QWebEngineScript = None  # type: ignore[misc, assignment]
    _WEBENGINE_AVAILABLE = False

# One shared disk-backed profile for the workspace browser (cookies, localStorage, IndexedDB).
_workspace_browser_profile_singleton: QWebEngineProfile | None = None

_SCROLLBAR_SCRIPT_NAME = "agent_workspace_scrollbars"
_TINT_SCRIPT_NAME = "agent_workspace_tint"


def _hex_to_hue_rotate(hex_color: str) -> float:
    """Hue-rotate (deg) that turns the sepia base toward the accent hue.
    Ported from vispy_dashboard/widgets/tv_player.py so the browser tint matches
    the dashboard/TV color treatment."""
    import colorsys
    h = hex_color.lstrip("#")
    if len(h) != 6:
        return 0.0
    try:
        r = int(h[0:2], 16) / 255.0
        g = int(h[2:4], 16) / 255.0
        b = int(h[4:6], 16) / 255.0
    except ValueError:
        return 0.0
    hue, _l, _s = colorsys.rgb_to_hls(r, g, b)
    hue_rotate = hue * 360.0 - 40.0  # 40° = sepia base hue
    while hue_rotate < 0:
        hue_rotate += 360
    while hue_rotate >= 360:
        hue_rotate -= 360
    return hue_rotate


def _browser_tint_enabled() -> bool:
    """Monocolor Browser applies the accent filter when Monocolor is on too."""
    try:
        from core.agent import load_config
        cfg = load_config()
        if not cfg.get("monocolor", True):
            return False
        if "monocolor_browser" in cfg:
            return bool(cfg.get("monocolor_browser", True))
        return bool(cfg.get("browser_tint", True))
    except Exception:
        return True


def _install_workspace_tint_user_script(prof: QWebEngineProfile, p: dict) -> None:
    """Impose the UI accent color over every page the browser loads — the same
    grayscale→sepia→hue-rotate→saturate→brightness filter the dashboard puts
    over its TV/charts. Images, video, canvas, and SVG are exempted so media
    still reads naturally. Injected at document-creation so there's no flash of
    untinted content. Safe to call again after a theme change."""
    if QWebEngineScript is None:
        return
    import json

    coll = prof.scripts()
    try:
        existing_scripts = coll.toList()
    except AttributeError:
        try:
            existing_scripts = list(coll)
        except TypeError:
            existing_scripts = []
    for existing in existing_scripts:
        if existing.name() == _TINT_SCRIPT_NAME:
            coll.remove(existing)
            break

    # Tint disabled → remove the script and stop (re-enabling re-injects).
    if not _browser_tint_enabled():
        return

    accent = p.get("accent", "#00ff00")
    hue = _hex_to_hue_rotate(accent)
    filter_css = (
        f"grayscale(100%) sepia(100%) hue-rotate({hue:.2f}deg) "
        "saturate(1.6) brightness(0.9)"
    )
    css = (
        "html{filter:" + filter_css + " !important;}"
        # Re-expose media to its natural color (it would otherwise double-filter).
        "img,video,canvas,svg,picture,iframe{filter:none !important;}"
    )
    payload = json.dumps(css)
    # MutationObserver re-asserts the <style> if a page (re)writes <html>/<head>.
    js = (
        "(function(){"
        "var ID='__agent_workspace_tint';"
        "function inject(){"
        "  var t=document.head||document.documentElement; if(!t)return;"
        "  if(document.getElementById(ID))return;"
        "  var s=document.createElement('style'); s.id=ID;"
        f" s.textContent={payload};"
        "  t.appendChild(s);"
        "}"
        "inject();"
        "document.addEventListener('DOMContentLoaded',inject);"
        "try{new MutationObserver(inject).observe("
        "document.documentElement||document,{childList:true,subtree:true});}catch(e){}"
        "})();"
    )
    script = QWebEngineScript()
    script.setName(_TINT_SCRIPT_NAME)
    script.setSourceCode(js)
    script.setInjectionPoint(QWebEngineScript.InjectionPoint.DocumentCreation)
    script.setWorldId(QWebEngineScript.ScriptWorldId.MainWorld)
    script.setRunsOnSubFrames(True)
    coll.insert(script)


def _workspace_browser_scrollbar_css(p: dict) -> str:
    """Chromium scrollbar styling aligned with ``glass_dialog`` / agent Qt scrollbars."""
    return f"""
html, body, * {{
  scrollbar-width: thin;
  scrollbar-color: {p['accent_muted']} {p['panel']};
}}
::-webkit-scrollbar {{
  width: 10px;
  height: 10px;
}}
::-webkit-scrollbar-track {{
  background: {p['panel']};
  border: 1px solid {p['border']};
}}
::-webkit-scrollbar-thumb {{
  background: {p['accent_muted']};
  min-height: 20px;
  min-width: 20px;
  border-radius: 2px;
}}
::-webkit-scrollbar-thumb:hover {{
  background: {p['accent']};
}}
::-webkit-scrollbar-corner {{
  background: {p['panel']};
}}
"""


def _install_workspace_scrollbar_user_script(prof: QWebEngineProfile, p: dict) -> None:
    """Inject scrollbar CSS on every document (matches agent UI). Safe to call again after theme change."""
    if QWebEngineScript is None:
        return
    import json

    css = _workspace_browser_scrollbar_css(p)
    payload = json.dumps(css)
    js = (
        "(function(){"
        "if(document.getElementById('__agent_workspace_scrollbars'))return;"
        "var s=document.createElement('style');"
        "s.id='__agent_workspace_scrollbars';"
        f"s.textContent={payload};"
        "(document.head||document.documentElement).appendChild(s);"
        "})();"
    )

    coll = prof.scripts()
    # PyQt6 builds differ: findScript() may be missing; iterate instead.
    try:
        existing_scripts = coll.toList()
    except AttributeError:
        try:
            existing_scripts = list(coll)
        except TypeError:
            existing_scripts = []
    for existing in existing_scripts:
        if existing.name() == _SCROLLBAR_SCRIPT_NAME:
            coll.remove(existing)
            break

    script = QWebEngineScript()
    script.setName(_SCROLLBAR_SCRIPT_NAME)
    script.setSourceCode(js)
    script.setInjectionPoint(QWebEngineScript.InjectionPoint.DocumentReady)
    script.setWorldId(QWebEngineScript.ScriptWorldId.MainWorld)
    script.setRunsOnSubFrames(True)
    coll.insert(script)


def get_workspace_browser_profile() -> QWebEngineProfile | None:
    """Lazily create a persistent QWebEngineProfile under ``data/webengine_workspace/``."""
    global _workspace_browser_profile_singleton
    if not _WEBENGINE_AVAILABLE:
        return None
    if _workspace_browser_profile_singleton is not None:
        return _workspace_browser_profile_singleton
    root = Path(__file__).resolve().parent.parent
    base = root / "data" / "webengine_workspace"
    storage = base / "storage"
    cache = base / "cache"
    storage.mkdir(parents=True, exist_ok=True)
    cache.mkdir(parents=True, exist_ok=True)
    app = QApplication.instance()
    parent = app if app is not None else None
    prof = QWebEngineProfile("agent_workspace_browser", parent)
    prof.setPersistentStoragePath(str(storage))
    prof.setCachePath(str(cache))
    try:
        prof.setHttpCacheType(QWebEngineProfile.HttpCacheType.DiskHttpCache)
    except Exception:
        pass
    try:
        prof.setPersistentCookiesPolicy(
            QWebEngineProfile.PersistentCookiesPolicy.AllowPersistentCookies
        )
    except Exception:
        pass

    def _flush_cookies() -> None:
        try:
            prof.cookieStore().flushCookies()
        except Exception:
            pass

    if app is not None:
        app.aboutToQuit.connect(_flush_cookies)

    _install_workspace_scrollbar_user_script(prof, PALETTE)
    _install_workspace_tint_user_script(prof, PALETTE)

    _workspace_browser_profile_singleton = prof
    return prof


if _WEBENGINE_AVAILABLE:

    class WorkspaceWebEnginePage(QWebEnginePage):
        """WebEngine page that opens OAuth / window.open in a real window or a tab."""

        def __init__(
            self,
            browser_panel: "BrowserWorkspacePanel",
            parent_view: QWebEngineView,
            profile: QWebEngineProfile | None = None,
        ):
            if profile is not None:
                super().__init__(profile, parent_view)
            else:
                super().__init__(parent_view)
            self._browser_panel = browser_panel
            self.featurePermissionRequested.connect(self._on_feature_permission)

        def _on_feature_permission(self, security_origin: QUrl, feature) -> None:
            self.setFeaturePermission(
                security_origin,
                feature,
                QWebEnginePage.PermissionPolicy.PermissionGrantedByUser,
            )

        def createWindow(self, wintype: QWebEnginePage.WebWindowType):
            panel = self._browser_panel
            opener_profile = self.profile()
            # OAuth / GSI expects a top-level popup with the same storage partition as the opener.
            if wintype in (
                QWebEnginePage.WebWindowType.WebDialog,
                QWebEnginePage.WebWindowType.WebBrowserWindow,
            ):
                return panel._open_web_popup_window(opener_profile)
            view = QWebEngineView(panel._tab_widget)
            new_page = WorkspaceWebEnginePage(panel, view, opener_profile)
            view.setPage(new_page)
            panel._apply_web_engine_settings(new_page)
            if wintype == QWebEnginePage.WebWindowType.WebBrowserBackgroundTab:
                title = "Background"
            else:
                title = "Popup"
            select = wintype != QWebEnginePage.WebWindowType.WebBrowserBackgroundTab
            panel._register_new_tab(view, url_to_load=None, title=title, select_tab=select)
            return new_page


def workspace_tab_bar_stylesheet(p: dict) -> str:
    """Match FileViewer tab styling (flat chrome tabs)."""
    return f"""
        QTabWidget::pane {{
            border: none;
            background: {p['panel_alt']};
        }}
        QTabBar::tab {{
            background: {p['panel']};
            color: {p['muted_text']};
            border: 1px solid {p['border']};
            border-bottom: none;
            padding: 3px 10px;
            margin-right: 1px;
            font-family: Consolas;
            font-size: 8pt;
        }}
        QTabBar::tab:selected {{
            background: {p['panel_alt']};
            color: {p['accent']};
            border-bottom: 2px solid {p['accent']};
        }}
        QTabBar::tab:hover {{
            color: {p['accent_bright']};
        }}
    """


def _page_toolbar_btn_stylesheet(p: dict, checked: bool) -> str:
    """Edged toggle buttons with richer hover/active feedback."""
    ac = p["accent"]
    hover_bg = p.get("accent_soft", p["panel_alt"])
    hover_fg = p.get("accent_bright", ac)
    pressed_bg = p.get("panel_alt", p["panel"])
    if checked:
        return (
            "QPushButton{"
            f"color:{p['background']};background:{ac};"
            f"border:1px solid {ac};border-radius:0;padding:4px 14px;"
            f"font-family:Consolas;font-size:8pt;"
            "}"
            "QPushButton:hover{"
            f"color:{p['background']};background:{hover_fg};border:1px solid {hover_fg};"
            "}"
            "QPushButton:pressed{"
            f"color:{p['background']};background:{ac};border:1px solid {ac};"
            "}"
        )
    return (
        "QPushButton{"
        f"color:{p['muted_text']};background:{p['panel']};"
        f"border:1px solid {p['border']};border-radius:0;padding:4px 14px;"
        f"font-family:Consolas;font-size:8pt;"
        "}"
        "QPushButton:hover{"
        f"color:{hover_fg};background:{hover_bg};border:1px solid {ac};"
        "}"
        "QPushButton:pressed{"
        f"color:{p['text']};background:{pressed_bg};border:1px solid {ac};"
        "}"
        "QPushButton:checked{"
        f"color:{p['background']};background:{ac};border:1px solid {ac};"
        "}"
    )


def _workspace_scrollbar_qss(p: dict) -> str:
    return (
        f"QScrollBar:vertical{{background:{p['panel']};width:10px;border:1px solid {p['border']};margin:0px;}}"
        f"QScrollBar::handle:vertical{{background:{p['accent_muted']};min-height:20px;border-radius:2px;}}"
        f"QScrollBar::handle:vertical:hover{{background:{p['accent']};}}"
        "QScrollBar::add-line:vertical,QScrollBar::sub-line:vertical{height:0px;border:none;background:transparent;}"
        "QScrollBar::add-page:vertical,QScrollBar::sub-page:vertical{background:transparent;}"
        f"QScrollBar:horizontal{{background:{p['panel']};height:10px;border:1px solid {p['border']};margin:0px;}}"
        f"QScrollBar::handle:horizontal{{background:{p['accent_muted']};min-width:20px;border-radius:2px;}}"
        f"QScrollBar::handle:horizontal:hover{{background:{p['accent']};}}"
        "QScrollBar::add-line:horizontal,QScrollBar::sub-line:horizontal{width:0px;border:none;background:transparent;}"
        "QScrollBar::add-page:horizontal,QScrollBar::sub-page:horizontal{background:transparent;}"
    )


class BrowserWorkspacePanel(QFrame):
    """Tabbed embedded browser (WebEngine) or install hint if WebEngine is missing."""

    def minimumSizeHint(self) -> QSize:
        # Override so the splitter doesn't honor the WebEngine view + nav
        # toolbar's implicit minimum (several hundred px). Callers can drag
        # this panel to arbitrary narrow widths without it snapping back.
        base = super().minimumSizeHint()
        return QSize(0, base.height())

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("BrowserWorkspacePanel")
        self.setMinimumWidth(0)
        self.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Expanding)
        self._tabs: list[dict] = []
        self._conv_tabs: dict[str, int] = {}   # conv_id -> tab index in _tabs
        self._page_contexts: dict[str, dict] = {}  # url -> {title, text}
        self._webengine = _WEBENGINE_AVAILABLE
        self._browser_profile: QWebEngineProfile | None = None
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        p = PALETTE
        nav = QHBoxLayout()
        nav.setContentsMargins(8, 4, 8, 4)
        nav.setSpacing(4)

        self._back_btn = QPushButton("<")
        self._fwd_btn = QPushButton(">")
        self._reload_btn = QPushButton("Reload")
        for b in (self._back_btn, self._fwd_btn, self._reload_btn):
            b.setFont(QFont("Consolas", 8))
            b.setCursor(Qt.CursorShape.PointingHandCursor)
            b.setStyleSheet(
                f"color:{p['accent']};background:{p['panel']};"
                f"border:1px solid {p['border']};border-radius:0;padding:2px 8px;"
            )
        self._back_btn.setFixedWidth(28)
        self._fwd_btn.setFixedWidth(28)
        self._back_btn.clicked.connect(self._on_back)
        self._fwd_btn.clicked.connect(self._on_forward)
        self._reload_btn.clicked.connect(self._on_reload)
        nav.addWidget(self._back_btn)
        nav.addWidget(self._fwd_btn)
        nav.addWidget(self._reload_btn)

        self._url_edit = QLineEdit()
        self._url_edit.setFont(QFont("Consolas", 9))
        self._url_edit.setPlaceholderText("https://…")
        self._url_edit.setStyleSheet(
            f"background:{p['panel_alt']};color:{p['text']};"
            f"border:1px solid {p['border']};border-radius:0;padding:2px 6px;"
        )
        self._url_edit.returnPressed.connect(self._on_go)
        nav.addWidget(self._url_edit, stretch=1)

        self._go_btn = QPushButton("Go")
        self._go_btn.setFont(QFont("Consolas", 8))
        self._go_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._go_btn.setStyleSheet(
            f"color:{p['accent']};background:{p['panel']};"
            f"border:1px solid {p['border']};border-radius:0;padding:2px 10px;"
        )
        self._go_btn.clicked.connect(self._on_go)
        nav.addWidget(self._go_btn)

        self._new_tab_btn = QPushButton("+ Tab")
        self._new_tab_btn.setFont(QFont("Consolas", 8))
        self._new_tab_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._new_tab_btn.setStyleSheet(
            f"color:{p['accent']};background:{p['panel']};"
            f"border:1px solid {p['border']};border-radius:0;padding:2px 8px;"
        )
        self._new_tab_btn.clicked.connect(self._add_blank_tab)
        nav.addWidget(self._new_tab_btn)

        self._close_btn = QPushButton("\u2715")
        self._close_btn.setFont(QFont("Consolas", 10))
        self._close_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._close_btn.setFixedWidth(22)
        self._close_btn.setStyleSheet(f"color:{p['muted_text']};background:transparent;border:none;")
        self._close_btn.clicked.connect(self._request_collapse)
        nav.addWidget(self._close_btn)

        self._nav_w = QWidget()
        self._nav_w.setLayout(nav)
        self._nav_w.setStyleSheet(f"background:{p['panel']};border-bottom:1px solid {p['border']};")
        # Let the nav bar shrink past its preferred width; the URL edit clips,
        # buttons stay visible. Without this the toolbar locks the panel's
        # horizontal minimum to ~sum(button widths) + URL edit minimum.
        self._nav_w.setMinimumWidth(0)
        self._url_edit.setMinimumWidth(0)
        layout.addWidget(self._nav_w)

        self._tab_widget = ThemedClosableTabWidget()
        self._tab_widget.setFont(QFont("Consolas", 8))
        self._tab_widget.setTabsClosable(True)
        self._tab_widget.setMinimumWidth(0)
        self._tab_widget.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Expanding)
        self._tab_widget.tabCloseRequested.connect(self._close_tab)
        self._tab_widget.currentChanged.connect(self._on_tab_changed)
        self._apply_tab_styles()
        layout.addWidget(self._tab_widget, stretch=1)

        self._collapse_cb = None
        self._placeholder: QLabel | None = None

        # Lazy: defer WebEngine profile init and the DuckDuckGo tab until the user
        # (or agent) opens the workspace "Browser" page — not on QStackedWidget
        # parent show, which still delivers showEvent for non-current pages on
        # some setups and adds startup cost + console noise from DDG preloads.
        self._initial_tab_pending = self._webengine
        self._pending_restore: dict | None = None
        if not self._webengine:
            hint = QLabel(
                "Embedded browser needs PyQt6 WebEngine.\n\n"
                "Install:  pip install PyQt6-WebEngine-Qt6\n\n"
                "Then restart the app."
            )
            hint.setWordWrap(True)
            hint.setFont(QFont("Consolas", 9))
            hint.setStyleSheet(f"color:{p['muted_text']};padding:16px;background:{p['panel_alt']};")
            hint.setAlignment(Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignLeft)
            self._placeholder = hint
            self._tab_widget.addTab(hint, "Browser")
            self._back_btn.setEnabled(False)
            self._fwd_btn.setEnabled(False)
            self._reload_btn.setEnabled(False)
            self._url_edit.setEnabled(False)
            self._go_btn.setEnabled(False)
            self._new_tab_btn.setEnabled(False)

    def set_collapse_callback(self, cb):
        self._collapse_cb = cb

    def has_embedded_browser(self) -> bool:
        return bool(self._webengine)

    def _ensure_lazy_default_tab(self) -> None:
        """Create profile + default search tab on first visit to the Browser page.
        If restore_state() deferred saved tabs, drain those first and skip the
        default-search tab."""
        if not self._webengine or not getattr(self, "_initial_tab_pending", False):
            return
        try:
            if self._browser_profile is None:
                self._browser_profile = get_workspace_browser_profile()
            restored = self._apply_pending_restore()
            if not restored:
                self._add_tab_with_url("https://duckduckgo.com", title="Search")
        except Exception:
            # Avoid tight loops if WebEngine/profile fails to initialize.
            self._initial_tab_pending = False

    def focus_agent_tool_url(self, url: str) -> bool:
        """Reuse or create a tab named *Agent* and load *url* (from browser automation tools)."""
        if not self._webengine:
            return False
        url = self._normalize_url(url or "")
        if not url:
            return False
        for i in range(self._tab_widget.count()):
            if self._tab_widget.tabText(i) != "Agent":
                continue
            if i >= len(self._tabs):
                continue
            self._tab_widget.setCurrentIndex(i)
            self._tabs[i]["view"].setUrl(QUrl(url))
            self._url_edit.setText(url)
            self._sync_nav_buttons()
            return True
        self._add_tab_with_url(url, title="Agent")
        return True

    def _request_collapse(self):
        if self._collapse_cb:
            self._collapse_cb()

    def _apply_tab_styles(self):
        self._tab_widget.setStyleSheet(workspace_tab_bar_stylesheet(PALETTE))
        self._tab_widget.set_close_palette(PALETTE)

    def _current_view(self):
        if not self._webengine:
            return None
        idx = self._tab_widget.currentIndex()
        if 0 <= idx < len(self._tabs):
            return self._tabs[idx].get("view")
        return None

    def _normalize_url(self, text: str) -> str:
        t = text.strip()
        if not t:
            return ""
        if "://" not in t:
            return "https://" + t
        return t

    def _on_go(self):
        if not self._webengine:
            return
        url = self._normalize_url(self._url_edit.text())
        if not url:
            return
        self._url_edit.setText(url)
        v = self._current_view()
        if v is not None:
            v.setUrl(QUrl(url))

    def _on_back(self):
        v = self._current_view()
        if v and v.page().history().canGoBack():
            v.back()

    def _on_forward(self):
        v = self._current_view()
        if v and v.page().history().canGoForward():
            v.forward()

    def _on_reload(self):
        v = self._current_view()
        if v:
            v.reload()

    def _reload_all_tabs_for_tint(self):
        """Reload every open tab so a just-changed tint hue (or its removal)
        takes effect — profile scripts only run on the next document load."""
        for tab in self._tabs:
            v = tab.get("view")
            if v is not None:
                try:
                    v.reload()
                except Exception:
                    pass

    def _add_blank_tab(self):
        self._add_tab_with_url("about:blank", title="New")

    def _open_web_popup_window(self, profile: QWebEngineProfile) -> QWebEnginePage:
        """Show window.open / OAuth UI in a top-level window (not a docked tab)."""
        parent_win = self.window()
        dlg = QDialog(parent_win)
        dlg.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, True)
        dlg.setModal(False)
        dlg.setWindowTitle("Sign-in")
        dlg.setWindowFlags(Qt.WindowType.Window | Qt.WindowType.WindowCloseButtonHint)
        view = QWebEngineView(dlg)
        new_page = WorkspaceWebEnginePage(self, view, profile)
        view.setPage(new_page)
        self._apply_web_engine_settings(new_page)
        new_page.windowCloseRequested.connect(dlg.accept)
        layout = QVBoxLayout(dlg)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        layout.addWidget(view)
        view.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        dlg.resize(520, 720)
        dlg.show()
        dlg.raise_()
        dlg.activateWindow()
        return new_page

    def _apply_web_engine_settings(self, page: QWebEnginePage) -> None:
        s = page.settings()
        s.setAttribute(QWebEngineSettings.WebAttribute.JavascriptEnabled, True)
        s.setAttribute(QWebEngineSettings.WebAttribute.JavascriptCanOpenWindows, True)
        s.setAttribute(QWebEngineSettings.WebAttribute.LocalStorageEnabled, True)
        try:
            s.setAttribute(QWebEngineSettings.WebAttribute.LocalContentCanAccessRemoteUrls, True)
        except Exception:
            pass
        try:
            s.setAttribute(QWebEngineSettings.WebAttribute.PdfViewerEnabled, True)
        except Exception:
            pass

    def _wire_browser_view_signals(self, view: QWebEngineView) -> None:
        view.urlChanged.connect(self._sync_url_from_view)
        view.loadFinished.connect(lambda _ok: self._sync_nav_buttons())
        view.urlChanged.connect(lambda _u: self._sync_nav_buttons())
        view.loadFinished.connect(lambda ok, v=view: self._capture_page_context(v) if ok else None)

    def _register_new_tab(
        self,
        view: QWebEngineView,
        url_to_load: str | None,
        title: str,
        select_tab: bool,
    ) -> None:
        # Ignored horizontal policy + min width 0 lets the view shrink with
        # the splitter. Default WebEngine views claim ~300px minimum.
        view.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Expanding)
        view.setMinimumWidth(0)
        self._wire_browser_view_signals(view)
        url = url_to_load or ""
        tab_info = {"view": view, "url": url}
        self._tabs.append(tab_info)
        short = title or url.replace("https://", "").replace("http://", "")[:24] or "Tab"
        i = self._tab_widget.addTab(view, short[:32])
        if select_tab:
            self._tab_widget.setCurrentIndex(i)
        if url_to_load:
            view.setUrl(QUrl(url_to_load))
            self._url_edit.setText(url_to_load)
        self._sync_nav_buttons()

    def _add_tab_with_url(self, url: str, title: str = ""):
        if not self._webengine:
            return
        prof = self._browser_profile or get_workspace_browser_profile()
        if prof is None:
            return
        if self._browser_profile is None:
            self._browser_profile = prof
        # Any explicit tab creation ends the lazy placeholder state.
        self._initial_tab_pending = False
        view = QWebEngineView(self._tab_widget)
        page = WorkspaceWebEnginePage(self, view, prof)
        view.setPage(page)
        self._apply_web_engine_settings(page)
        self._register_new_tab(view, url_to_load=url, title=title, select_tab=True)

    def _close_tab(self, index: int):
        if not self._webengine:
            return
        if index < 0 or index >= len(self._tabs):
            return
        tab = self._tabs.pop(index)
        self._tab_widget.removeTab(index)
        tab["view"].deleteLater()
        # Keep _conv_tabs indices in sync — remove any stale entry, shift others
        to_remove = [k for k, v in self._conv_tabs.items() if v == index]
        for k in to_remove:
            del self._conv_tabs[k]
        # Shift tab indices that were above the closed one
        self._conv_tabs = {k: v - 1 if v > index else v for k, v in self._conv_tabs.items()}
        if not self._tabs:
            self._add_tab_with_url("about:blank", title="New")
        self._sync_nav_buttons()

    def get_or_create_for_conv(self, conv_id: str, conv_name: str, url: str = "") -> None:
        """Navigate the conversation's browser tab, creating it if needed.

        If the conversation already has a tab, navigate it to *url*.
        Otherwise create a new tab named after the conversation.
        """
        if not self._webengine:
            return
        existing_idx = self._conv_tabs.get(conv_id)
        if existing_idx is not None and 0 <= existing_idx < len(self._tabs):
            view = self._tabs[existing_idx]["view"]
            self._tab_widget.setCurrentIndex(existing_idx)
            if url:
                view.setUrl(QUrl(url))
                self._url_edit.setText(url)
            self._sync_nav_buttons()
            return
        # Create a new tab for this conversation
        label = (conv_name or conv_id or "Agent")[:28]
        self._add_tab_with_url(url or "about:blank", title=label)
        new_idx = len(self._tabs) - 1
        self._conv_tabs[conv_id] = new_idx
        # Hook URL-change → page context capture
        view = self._tabs[new_idx]["view"]
        view.loadFinished.connect(lambda ok, v=view: self._capture_page_context(v))

    def switch_to_conv(self, conv_id: str):
        """Make the conversation's tab active (noop if none exists)."""
        idx = self._conv_tabs.get(conv_id)
        if idx is not None and 0 <= idx < len(self._tabs):
            self._tab_widget.setCurrentIndex(idx)

    def _capture_page_context(self, view) -> None:
        """Schedule a delayed capture so SPAs (Twitter, Reddit, etc.) have time to render."""
        # 1.5 s lets React/Next.js apps finish their initial render before we read the DOM
        QTimer.singleShot(1500, lambda: self._do_capture(view))

    def _do_capture(self, view) -> None:
        """Extract URL, title, text and a screenshot from *view* into the context cache."""
        url = view.url().toString()
        if not url or url in ("about:blank", ""):
            return
        title = view.title() or url
        screenshot_path = self._grab_screenshot(view)
        view.page().runJavaScript(
            "document.body ? document.body.innerText.slice(0, 8000) : ''",
            lambda text, u=url, t=title, sp=screenshot_path:
                self._store_page_context(u, t, text or "", sp),
        )

    def _grab_screenshot(self, view) -> str:
        """Capture the current QWebEngineView as a JPEG and return the file path."""
        try:
            import tempfile, os
            pixmap = view.grab()
            if pixmap.isNull():
                return ""
            path = os.path.join(
                tempfile.gettempdir(),
                f"ws_browser_{abs(hash(view.url().toString())) % 100000}.jpg",
            )
            pixmap.save(path, "JPEG", 85)
            return path
        except Exception:
            return ""

    def _store_page_context(self, url: str, title: str, text: str, screenshot_path: str = "") -> None:
        self._page_contexts[url] = {
            "url": url,
            "title": title,
            "text": text,
            "screenshot_path": screenshot_path,
        }
        # Keep cache bounded
        if len(self._page_contexts) > 50:
            oldest = next(iter(self._page_contexts))
            del self._page_contexts[oldest]

    def get_current_page_context(self) -> dict:
        """Return {url, title, text, screenshot_path} for the active tab, or {}."""
        idx = self._tab_widget.currentIndex()
        if not self._webengine or idx < 0 or idx >= len(self._tabs):
            return {}
        url = self._tabs[idx]["view"].url().toString()
        return self._page_contexts.get(
            url, {"url": url, "title": "", "text": "", "screenshot_path": ""}
        )

    def grab_current_view(self):
        """Capture the active tab as a QPixmap. Called on the main thread only."""
        if not self._webengine:
            return None
        idx = self._tab_widget.currentIndex()
        if idx < 0 or idx >= len(self._tabs):
            return None
        pixmap = self._tabs[idx]["view"].grab()
        return pixmap if not pixmap.isNull() else None

    def _on_tab_changed(self, idx: int):
        if not self._webengine or idx < 0 or idx >= len(self._tabs):
            return
        v = self._tabs[idx]["view"]
        self._url_edit.setText(v.url().toString() or "")
        self._sync_nav_buttons()

    def _sync_url_from_view(self, qurl: QUrl):
        v = self.sender()
        if v is not self._current_view():
            return
        self._url_edit.setText(qurl.toString())

    def _sync_nav_buttons(self):
        v = self._current_view()
        if not v:
            self._back_btn.setEnabled(False)
            self._fwd_btn.setEnabled(False)
            return
        h = v.page().history()
        self._back_btn.setEnabled(h.canGoBack())
        self._fwd_btn.setEnabled(h.canGoForward())

    def get_state(self) -> dict:
        if not self._webengine:
            return {"urls": [], "active": 0}
        urls = []
        for t in self._tabs:
            u = t["view"].url().toString()
            urls.append(u or t.get("url", ""))
        return {"urls": urls, "active": self._tab_widget.currentIndex()}

    def restore_state(self, state: dict | None):
        self.close_all_tabs()
        if not self._webengine:
            return
        # Defer all tab restoration until the user actually opens the Browser
        # page. Otherwise startup spins up the QWebEngine profile, loads every
        # saved URL, and hits the network before the user has even expressed
        # interest in browsing this session.
        if not state:
            self._initial_tab_pending = True
            self._pending_restore = None
            return
        urls = [u for u in (state.get("urls") or []) if (u or "").strip()]
        try:
            active = int(state.get("active", 0) or 0)
        except (TypeError, ValueError):
            active = 0
        if not urls:
            self._initial_tab_pending = True
            self._pending_restore = None
            return
        self._pending_restore = {"urls": urls, "active": active}
        self._initial_tab_pending = True

    def _apply_pending_restore(self) -> bool:
        """Drain any deferred restore_state() payload. Returns True if tabs
        were created (so the caller can skip the default-search tab)."""
        pending = getattr(self, "_pending_restore", None)
        if not pending:
            return False
        self._pending_restore = None
        for i, u in enumerate(pending["urls"]):
            self._add_tab_with_url(u, title=f"Tab {i + 1}")
        active = pending.get("active", 0) or 0
        if 0 <= active < self._tab_widget.count():
            self._tab_widget.setCurrentIndex(active)
        return self._tab_widget.count() > 0

    def close_all_tabs(self):
        if not self._webengine:
            return
        for t in self._tabs:
            t["view"].deleteLater()
        self._tabs.clear()
        self._conv_tabs.clear()
        self._tab_widget.clear()
        self._initial_tab_pending = True

    def apply_theme(self):
        p = PALETTE
        self.setStyleSheet(
            f"""
            QFrame#BrowserWorkspacePanel {{
                background: {p['panel_alt']};
                border: none;
            }}
            """
        )
        self._nav_w.setStyleSheet(f"background:{p['panel']};border-bottom:1px solid {p['border']};")
        for b in (self._back_btn, self._fwd_btn, self._reload_btn):
            b.setStyleSheet(
                f"color:{p['accent']};background:{p['panel']};"
                f"border:1px solid {p['border']};border-radius:0;padding:2px 8px;"
            )
        self._url_edit.setStyleSheet(
            f"background:{p['panel_alt']};color:{p['text']};"
            f"border:1px solid {p['border']};border-radius:0;padding:2px 6px;"
        )
        self._go_btn.setStyleSheet(
            f"color:{p['accent']};background:{p['panel']};"
            f"border:1px solid {p['border']};border-radius:0;padding:2px 10px;"
        )
        self._new_tab_btn.setStyleSheet(
            f"color:{p['accent']};background:{p['panel']};"
            f"border:1px solid {p['border']};border-radius:0;padding:2px 8px;"
        )
        self._close_btn.setStyleSheet(f"color:{p['muted_text']};background:transparent;border:none;")
        self._apply_tab_styles()
        if self._browser_profile is not None:
            _install_workspace_scrollbar_user_script(self._browser_profile, p)
            _install_workspace_tint_user_script(self._browser_profile, p)
            # Re-tint already-open tabs immediately — injected scripts only run
            # on the next document load, so reload current pages to apply the
            # new hue (or remove the tint if it was just disabled).
            self._reload_all_tabs_for_tint()


class RightWorkspacePanel(QFrame):
    """Toolbar (Notes | Calendar | Browser | File | Terminal) + stacked pages."""

    def minimumSizeHint(self) -> QSize:
        # The splitter should honor the user's desired width, not a minimum
        # computed from whichever inner page (Browser / Terminal / Files)
        # currently has the widest implicit hint. Return 0 width.
        base = super().minimumSizeHint()
        return QSize(0, base.height())

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("RightWorkspacePanel")
        self.setMinimumWidth(0)
        self.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Expanding)
        p = PALETTE
        self.setStyleSheet(
            f"""
            QFrame#RightWorkspacePanel {{
                background: {p['panel_alt']};
                border: 1px solid {p['border']};
            }}
            {_workspace_scrollbar_qss(p)}
            """
        )

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        page_bar = QHBoxLayout()
        page_bar.setContentsMargins(0, 0, 0, 0)
        page_bar.setSpacing(0)

        # Page indices: 0 Notes, 1 Calendar, 2 Browser, 3 File, 4 Terminal
        self._btn_notes = QPushButton("Notes")
        self._btn_notes.setCheckable(True)
        self._btn_notes.setCursor(Qt.CursorShape.PointingHandCursor)
        self._btn_notes.setStyleSheet(_page_toolbar_btn_stylesheet(p, False))

        self._btn_calendar = QPushButton("Calendar")
        self._btn_calendar.setCheckable(True)
        self._btn_calendar.setCursor(Qt.CursorShape.PointingHandCursor)
        self._btn_calendar.setStyleSheet(_page_toolbar_btn_stylesheet(p, False))

        self._btn_browser = QPushButton("Browser")
        self._btn_browser.setCheckable(True)
        self._btn_browser.setCursor(Qt.CursorShape.PointingHandCursor)
        self._btn_browser.setStyleSheet(_page_toolbar_btn_stylesheet(p, False))

        self._btn_files = QPushButton("File")
        self._btn_files.setCheckable(True)
        self._btn_files.setChecked(True)
        self._btn_files.setCursor(Qt.CursorShape.PointingHandCursor)
        self._btn_files.setStyleSheet(_page_toolbar_btn_stylesheet(p, True))

        self._btn_terminal = QPushButton("Terminal")
        self._btn_terminal.setCheckable(True)
        self._btn_terminal.setCursor(Qt.CursorShape.PointingHandCursor)
        self._btn_terminal.setStyleSheet(_page_toolbar_btn_stylesheet(p, False))

        self._page_group = QButtonGroup(self)
        self._page_group.setExclusive(True)
        self._page_group.addButton(self._btn_notes, 0)
        self._page_group.addButton(self._btn_calendar, 1)
        self._page_group.addButton(self._btn_browser, 2)
        self._page_group.addButton(self._btn_files, 3)
        self._page_group.addButton(self._btn_terminal, 4)
        self._page_group.idClicked.connect(lambda i: self._set_page(i, from_user=True))

        page_bar.addStretch(1)
        page_bar.addWidget(self._btn_notes)
        page_bar.addWidget(self._btn_calendar)
        page_bar.addWidget(self._btn_browser)
        page_bar.addWidget(self._btn_files)
        page_bar.addWidget(self._btn_terminal)
        page_bar.addStretch(1)

        self._bar_w = QWidget()
        self._bar_w.setLayout(page_bar)
        self._bar_w.setStyleSheet(f"background:{p['panel']};border-bottom:1px solid {p['border']};")
        root.addWidget(self._bar_w)

        self._stack = QStackedWidget()
        self._stack.setMinimumWidth(0)
        self._stack.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Expanding)
        self.file_viewer = FileViewer()
        self.browser_panel = BrowserWorkspacePanel()
        self.terminal_panel = MultiConvTerminalPanel()
        self.notes_panel = NotesWorkspacePanel()
        self.calendar_panel = CalendarWorkspacePanel()
        # Ensure every child page can be shrunk by the splitter.
        for _w in (
            self.file_viewer,
            self.browser_panel,
            self.terminal_panel,
            self.notes_panel,
            self.calendar_panel,
        ):
            _w.setMinimumWidth(0)
            _w.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Expanding)

        self._stack.addWidget(self.notes_panel)       # 0
        self._stack.addWidget(self.calendar_panel)     # 1
        self._stack.addWidget(self.browser_panel)      # 2
        self._stack.addWidget(self.file_viewer)        # 3
        self._stack.addWidget(self.terminal_panel)     # 4
        # 5 — remote shell shown in place of the local terminal while mirroring.
        from ui.remote_terminal_view import RemoteTerminalWidget
        self.remote_terminal = RemoteTerminalWidget()
        self.remote_terminal.setMinimumWidth(0)
        self.remote_terminal.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Expanding)
        self._stack.addWidget(self.remote_terminal)
        self._remote_term_active = False
        root.addWidget(self._stack, stretch=1)

        self._collapse = lambda: None
        self.file_viewer._collapse_cb = self._collapse_panel
        self.browser_panel.set_collapse_callback(self._collapse_panel)
        self.terminal_panel.set_collapse_callback(self._collapse_panel)

    def _collapse_panel(self):
        self._collapse()

    def set_collapse_splitter_callback(self, cb):
        self._collapse = cb

    def _set_page(self, index: int, from_user: bool = False):
        p = PALETTE
        index = max(0, min(4, int(index)))
        # While mirroring, the Terminal button shows the REMOTE shell (page 5),
        # not the local terminal — the local one runs on this machine.
        if index == 4 and getattr(self, "_remote_term_active", False):
            self._stack.setCurrentWidget(self.remote_terminal)
            if from_user:
                QTimer.singleShot(0, self.remote_terminal.focus_active_input)
        else:
            self._stack.setCurrentIndex(index)
        self._btn_notes.setStyleSheet(_page_toolbar_btn_stylesheet(p, index == 0))
        self._btn_calendar.setStyleSheet(_page_toolbar_btn_stylesheet(p, index == 1))
        self._btn_browser.setStyleSheet(_page_toolbar_btn_stylesheet(p, index == 2))
        self._btn_files.setStyleSheet(_page_toolbar_btn_stylesheet(p, index == 3))
        self._btn_terminal.setStyleSheet(_page_toolbar_btn_stylesheet(p, index == 4))
        self._btn_notes.setChecked(index == 0)
        self._btn_calendar.setChecked(index == 1)
        self._btn_browser.setChecked(index == 2)
        self._btn_files.setChecked(index == 3)
        self._btn_terminal.setChecked(index == 4)
        # Only spin up the browser (profile + default search tab) on an actual
        # user click. Session restore hits this path too — without from_user it
        # would load duckduckgo on every startup whether or not the user ever
        # wanted the browser this session.
        if from_user and index == 2 and _WEBENGINE_AVAILABLE:
            self.browser_panel._ensure_lazy_default_tab()
            QTimer.singleShot(0, self._maybe_focus_browser_url)
        elif from_user and index == 4:
            QTimer.singleShot(0, self.terminal_panel.focus_active_input)

    def set_terminal_available(self, available: bool) -> None:
        """Show/hide the Terminal tool button."""
        try:
            self._btn_terminal.setVisible(available)
            if not available and self._stack.currentIndex() in (4, 5):
                self._set_page(3)
        except Exception:
            pass

    def enter_remote_terminal(self, peer_url: str, conv_id: str, peer_name: str) -> None:
        """Point the Terminal tool at a live shell on the host. The button stays
        visible and now opens the remote shell instead of the local one."""
        try:
            self._remote_term_active = True
            self.remote_terminal.connect_to(peer_url, conv_id, peer_name)
            self._btn_terminal.setVisible(True)
            if self._stack.currentIndex() in (4, 5):
                self._set_page(4)   # re-render as the remote page
        except Exception as e:
            print(f"[network] remote terminal unavailable: {e}", flush=True)

    def exit_remote_terminal(self) -> None:
        try:
            self._remote_term_active = False
            self.remote_terminal.disconnect_now()
            if self._stack.currentWidget() is self.remote_terminal:
                self._set_page(4)   # back to the local terminal
        except Exception:
            pass

    def _maybe_focus_browser_url(self):
        self.browser_panel._url_edit.setFocus()

    def current_workspace_page(self) -> int:
        return self._stack.currentIndex()

    def set_workspace_page(self, index: int):
        try:
            index = int(index)
        except (TypeError, ValueError):
            index = 0
        index = max(0, min(4, index))
        self._set_page(index)

    def flash_file_tab(self, times: int = 3) -> None:
        """Briefly flash the File tab button to signal a background agent edit
        without switching to it. Self-stopping; restores the button's style."""
        btn = getattr(self, "_btn_files", None)
        if btn is None:
            return
        from PyQt6.QtCore import QTimer as _QTimer
        p = PALETTE
        base_ss = btn.styleSheet()
        hot_ss = (base_ss + f"\nQPushButton {{ color:{p['glow_hot']};"
                  f" border-color:{p['accent_bright']}; }}")
        state = {"n": 0}

        def _tick():
            try:
                btn.setStyleSheet(hot_ss if state["n"] % 2 == 0 else base_ss)
            except Exception:
                return
            state["n"] += 1
            if state["n"] >= times * 2:
                try:
                    btn.setStyleSheet(base_ss)
                except Exception:
                    pass
                return
            _QTimer.singleShot(160, _tick)

        _tick()

    def apply_theme(self):
        p = PALETTE
        self.setStyleSheet(
            f"""
            QFrame#RightWorkspacePanel {{
                background: {p['panel_alt']};
                border: 1px solid {p['border']};
            }}
            {_workspace_scrollbar_qss(p)}
            """
        )
        idx = self._stack.currentIndex()
        self._btn_notes.setStyleSheet(_page_toolbar_btn_stylesheet(p, idx == 0))
        self._btn_calendar.setStyleSheet(_page_toolbar_btn_stylesheet(p, idx == 1))
        self._btn_browser.setStyleSheet(_page_toolbar_btn_stylesheet(p, idx == 2))
        self._btn_files.setStyleSheet(_page_toolbar_btn_stylesheet(p, idx == 3))
        self._btn_terminal.setStyleSheet(_page_toolbar_btn_stylesheet(p, idx == 4))
        self._bar_w.setStyleSheet(f"background:{p['panel']};border-bottom:1px solid {p['border']};")
        self.file_viewer.apply_theme()
        self.browser_panel.apply_theme()
        self.terminal_panel.apply_theme()
        self.notes_panel.apply_theme()
        self.calendar_panel.apply_theme()
