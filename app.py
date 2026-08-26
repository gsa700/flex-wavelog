#!/usr/bin/env python3
"""
app.py - desktop shell for Wavelog with the Waverider CAT bridge built in.

A single window hosting the Wavelog web UI, a preferences/status window reached
from the native menu, and the CAT bridge running on a background thread so CAT
publishing lives and dies with this process instead of a separate scheduled task.

Closing the main window minimises it rather than quitting: an X-click that
silently stopped CAT publishing mid-contest would be a nasty surprise. Quitting
is a deliberate act from the menu.
"""

import json
import logging
import logging.handlers
import os
import sys
import threading
import time
import webbrowser
from urllib.parse import urlparse

import webview
from webview.menu import Menu, MenuAction, MenuSeparator

import waverider as wr

HERE = os.path.dirname(os.path.abspath(__file__))
PREFS_HTML = os.path.join(HERE, "ui", "prefs.html")
ICON_PATH = os.path.join(HERE, "assets", "waverider.ico")
# WebView2 profile. Without a persistent store the Wavelog session is thrown
# away on exit and you log in again on every launch.
STORAGE_PATH = os.path.join(HERE, ".webview")
# Window geometry lives here, not in config.json: that file is the user's
# settings and goes through save_config()'s validation - UI state shouldn't.
UI_STATE_PATH = os.path.join(HERE, "ui_state.json")

log = logging.getLogger("waverider.app")


def load_ui_state():
    try:
        with open(UI_STATE_PATH, encoding="utf-8") as fh:
            return json.load(fh)
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}


def save_ui_state(**entries):
    state = load_ui_state()
    state.update(entries)
    try:
        with open(UI_STATE_PATH, "w", encoding="utf-8") as fh:
            json.dump(state, fh, indent=2)
    except OSError as err:
        log.warning("Could not write %s: %s", UI_STATE_PATH, err)


def position_visible(x, y, w, h):
    """Is enough of a window at (x, y) on the desktop to grab its title bar?

    A saved position is only trustworthy while the monitor layout that produced
    it exists - unplug a display and restored coordinates can land entirely
    off-screen, which reads as "the window doesn't open". Checked against the
    virtual screen (all monitors); anywhere but Windows just trust the values.
    """
    if sys.platform != "win32":
        return True
    import ctypes
    u = ctypes.windll.user32
    vx, vy = u.GetSystemMetrics(76), u.GetSystemMetrics(77)
    vw, vh = u.GetSystemMetrics(78), u.GetSystemMetrics(79)
    return (x + w > vx + 50 and x < vx + vw - 50
            and y >= vy - 8 and y < vy + vh - 50)


def set_window_icon(window):
    """Put our icon on a window's title bar and taskbar entry (Windows only).

    pywebview's `icon` parameter is GTK/QT-only, but on the edgechromium
    backend `window.native` is a WinForms Form whose Icon property we can set
    directly. The set is marshalled through Form.Invoke because the event that
    triggers this does not fire on the UI thread, and WinForms throws on
    cross-thread property access. Purely cosmetic, so failure is logged and
    swallowed rather than allowed to take the app down.
    """
    if sys.platform != "win32" or not os.path.exists(ICON_PATH):
        return
    try:
        import clr
        clr.AddReference("System.Drawing")
        from System import Action
        from System.Drawing import Icon

        form = window.native
        form.Invoke(Action(lambda: setattr(form, "Icon", Icon(ICON_PATH))))
    except Exception as err:
        log.warning("Could not set window icon: %s", err)


def hide_window_menu(window):
    """Remove the app menu bar from a secondary window (Windows only).

    pywebview applies the menu handed to webview.start() to every window it
    creates - per-window menus aren't in its API. A Setup dialog carrying a
    menu whose first item opens Setup is clutter, so on the WinForms backend
    we find the form's MenuStrip and hide it. Cosmetic like the icon: failure
    is logged and swallowed, and marshalled through Invoke for the same
    cross-thread reason.
    """
    if sys.platform != "win32":
        return
    try:
        import clr
        clr.AddReference("System.Windows.Forms")
        from System import Action
        from System.Windows.Forms import MenuStrip

        form = window.native

        def hide():
            for ctl in list(form.Controls):
                if isinstance(ctl, MenuStrip):
                    ctl.Visible = False

        form.Invoke(Action(hide))
    except Exception as err:
        log.warning("Could not hide the Setup window menu: %s", err)


def set_dpi_awareness():
    """Declare per-monitor DPI awareness before any window exists.

    Windows draws a non-aware process at 96 DPI and stretches the result. The
    visible symptom here was subtler: the first window looked fine until a
    second was created, at which point WebView2's layout viewport and the
    form's client area disagreed and the page collapsed into narrow columns.
    No-op anywhere but Windows.
    """
    if sys.platform != "win32":
        return
    import ctypes
    for attempt in (
        lambda: ctypes.windll.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4)),
        lambda: ctypes.windll.shcore.SetProcessDpiAwareness(2),
        lambda: ctypes.windll.user32.SetProcessDPIAware(),
    ):
        try:
            if attempt():
                return
        except Exception:
            continue


class Api:
    """Bound into the prefs page as pywebview.api.*"""

    def __init__(self, app):
        # Underscore-prefixed deliberately: pywebview walks the public attributes
        # of the js_api object to expose them, and a plain self.app would lead it
        # into App -> main_window -> .native (a WinForms Form) and recurse through
        # the .NET object graph until it blew the stack, taking the whole bridge
        # down with it. Private names are skipped.
        self._app = app

    def get_config(self):
        cfg = dict(self._app.cfg)
        # The token is never handed to the page. The field renders as a
        # placeholder and only a non-empty submission replaces what's stored.
        cfg["api_token"] = ""
        cfg["token_is_set"] = bool(self._app.cfg.get("api_token"))
        names = cfg.get("radio_names") or {}
        cfg["radio_A"] = names.get("A", "")
        cfg["radio_B"] = names.get("B", "")
        cfg["app_version"] = wr.__version__
        return cfg

    def open_repo(self):
        webbrowser.open("https://github.com/gsa700/waverider")

    def save_config(self, incoming):
        cfg = dict(self._app.cfg)

        backend = (incoming.get("radio_backend") or "flex").strip().lower()
        if backend not in wr.BACKENDS:
            return {"ok": False, "error": f"Unknown radio backend {backend!r}"}
        cfg["radio_backend"] = backend

        def text(key):
            return (incoming.get(key) or "").strip()

        # Only the active backend's fields are *required*; the inactive one's
        # keep their stored values so switching back doesn't forget them.
        required = ["wavelog_url"]
        required += ["flex_host"] if backend == "flex" else \
                    ["rigctld_host", "rigctld_radio_name"]
        for key in required:
            if not text(key):
                return {"ok": False, "error": f"{key} cannot be empty"}
        cfg["wavelog_url"] = text("wavelog_url")
        for key in ("flex_host", "rigctld_host", "rigctld_radio_name"):
            cfg[key] = text(key) or cfg.get(key)
        cfg["wsjtx_bind"] = text("wsjtx_bind") or "127.0.0.1"

        # An empty tx_radio_name is meaningful: it disables the follower entry.
        tx_name = (incoming.get("tx_radio_name") or "").strip()
        cfg["tx_radio_name"] = tx_name or None

        try:
            cfg["flex_port"] = int(incoming.get("flex_port") or 4992)
            cfg["rigctld_port"] = int(incoming.get("rigctld_port") or 4532)
            cfg["rigctld_poll_seconds"] = float(incoming.get("rigctld_poll_seconds") or 1)
            cfg["wsjtx_port"] = int(incoming.get("wsjtx_port") or 2237)
            cfg["station_profile_id"] = int(incoming.get("station_profile_id") or 1)
            cfg["heartbeat_seconds"] = float(incoming.get("heartbeat_seconds") or 5)
            cfg["min_post_interval"] = float(incoming.get("min_post_interval") or 1)
            cfg["amp_power_watts"] = int(incoming.get("amp_power_watts") or 0)
        except (TypeError, ValueError):
            return {"ok": False, "error": "Port, ID and interval fields must be numbers"}

        for key in ("send_power", "wsjtx_enabled", "auto_refresh_log"):
            cfg[key] = bool(incoming.get(key))

        names = {}
        for letter in ("A", "B"):
            value = (incoming.get(f"radio_{letter}") or "").strip()
            if value:
                names[letter] = value
        cfg["radio_names"] = names

        token = (incoming.get("api_token") or "").strip()
        if token:
            if not token.startswith(wr.TOKEN_PREFIX):
                return {"ok": False,
                        "error": f"Token must start with '{wr.TOKEN_PREFIX}' - "
                                 "a v1 key won't work against /api/v2/radio"}
            cfg["api_token"] = token
        if not cfg.get("api_token") or "PASTE_YOUR" in cfg["api_token"]:
            return {"ok": False, "error": "An API token is required"}

        # Strip the display-only keys before they reach disk.
        for key in ("token_is_set", "radio_A", "radio_B"):
            cfg.pop(key, None)

        with open(wr.CONFIG_PATH, "w", encoding="utf-8") as fh:
            json.dump(cfg, fh, indent=2)
        self._app.cfg = cfg
        self._app.restart_bridge()
        log.info("Config saved; bridge restarted")
        return {"ok": True}

    def get_status(self):
        bridge = self._app.bridge
        if bridge is None:
            return {"connected": False, "error": "bridge not running", "radios": {}}
        now = time.time()
        radios = {
            name: dict(info, age=round(now - info["updated"], 1))
            for name, info in bridge.status["radios"].items()
        }
        out = {
            "connected": bridge.status["connected"],
            "error": bridge.status["error"],
            "amp": bridge.status.get("amp"),
            "radios": radios,
        }
        if self._app.forwarder is not None:
            out["qso"] = dict(self._app.forwarder.status,
                              listening=self._app.listener.status["listening"])
        return out

    def quit(self):
        self._app.quit()


class App:
    def __init__(self):
        self.cfg = wr.load_config()
        self.api = Api(self)
        self.bridge = None
        self.thread = None
        self.forwarder = None
        self.listener = None
        self.main_window = None
        self.prefs_window = None
        self.quitting = False

    # -- bridge lifecycle -------------------------------------------------

    def start_bridge(self):
        self.bridge = wr.make_bridge(self.cfg)
        self.thread = threading.Thread(target=self.bridge.run, daemon=True,
                                       name="bridge")
        self.thread.start()
        self.forwarder = self.listener = None
        if self.cfg.get("wsjtx_enabled"):
            self.forwarder = wr.QsoForwarder(self.cfg)
            self.forwarder.on_delivered = self.on_qsos_delivered
            threading.Thread(target=self.forwarder.run, daemon=True,
                             name="qso-forwarder").start()
            self.listener = wr.WsjtxListener(self.cfg, self.forwarder)
            self.listener.start()

    def on_qsos_delivered(self, count):
        """Reload the main window after a QSO lands, so the visible log updates.

        Deliberately conservative about when: only on views that are read-only
        renderings of the log (dashboard, logbook) - reloading a page where the
        user might be mid-keystroke in a form field would eat their input. The
        QSO entry page is exactly the wrong place to reload, so it never
        matches. Runs on the forwarder thread; pywebview marshals both calls
        to the UI thread itself.
        """
        if not self.cfg.get("auto_refresh_log", True):
            return
        window = self.main_window
        if window is None or self.quitting:
            return
        try:
            url = window.get_current_url() or ""
            path = urlparse(url).path.rstrip("/").lower()
            base = path.rsplit("/", 1)[-1]
            # "" covers the site root, which Wavelog lands on the dashboard.
            if base in ("", "dashboard", "logbook", "index.php") or "logbook" in path:
                window.evaluate_js("location.reload()")
                log.info("Refreshed log view after %d QSO(s)", count)
        except Exception as err:
            log.warning("Log view refresh failed: %s", err)

    def stop_bridge(self):
        if self.bridge is not None:
            self.bridge.stop_requested = True
            # The reader sits in recv with a 1s timeout; closing the socket drops
            # it out immediately rather than waiting for the loop to notice.
            try:
                self.bridge.sock.close()
            except Exception:
                pass
        if self.listener is not None:
            self.listener.stop()
        if self.forwarder is not None:
            self.forwarder.stop()
        if self.thread is not None:
            self.thread.join(timeout=5)
        self.bridge = None
        self.thread = None
        self.forwarder = None
        self.listener = None

    def restart_bridge(self):
        self.stop_bridge()
        self.start_bridge()

    # -- windows ----------------------------------------------------------

    def open_in_browser(self):
        """Hand the Wavelog URL to the system default browser.

        The embedded engine is WebView2 (Chromium) and there is no Firefox
        equivalent to embed - Mozilla dropped embedding APIs years ago. So when
        a Wavelog admin screen misbehaves under Chromium, the escape hatch is to
        open it in the real browser rather than to change engines.
        """
        webbrowser.open(self.cfg["wavelog_url"])

    def open_prefs(self):
        if self.prefs_window is None:
            geo = load_ui_state().get("setup_window") or {}
            kwargs = {"width": geo.get("width", 820),
                      "height": geo.get("height", 760)}
            if ("x" in geo and "y" in geo and position_visible(
                    geo["x"], geo["y"], kwargs["width"], kwargs["height"])):
                kwargs["x"], kwargs["y"] = geo["x"], geo["y"]
            self.prefs_window = webview.create_window(
                "Setup", PREFS_HTML, js_api=self.api, **kwargs)
            self.prefs_window.events.closing += self._prefs_save_geometry
            self.prefs_window.events.closed += self._prefs_closed
            self.prefs_window.events.shown += (
                lambda: (set_window_icon(self.prefs_window),
                         hide_window_menu(self.prefs_window)))
        else:
            self.prefs_window.show()

    def _prefs_save_geometry(self):
        """Record where the user left the Setup window (closing, not closed:
        the native window still exists here, so its geometry is readable)."""
        window = self.prefs_window
        if window is None:
            return
        try:
            save_ui_state(setup_window={
                "x": int(window.x), "y": int(window.y),
                "width": int(window.width), "height": int(window.height),
            })
        except Exception as err:
            log.warning("Could not save Setup window position: %s", err)

    def _prefs_closed(self):
        self.prefs_window = None

    def on_main_closing(self):
        if self.quitting:
            return True
        self.main_window.minimize()
        return False        # cancel the close; CAT keeps publishing

    def quit(self):
        self.quitting = True
        self.stop_bridge()
        for window in list(webview.windows):
            try:
                window.destroy()
            except Exception:
                pass


def main():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        handlers=[logging.handlers.RotatingFileHandler(
            wr.LOG_PATH, maxBytes=1_000_000, backupCount=3, encoding="utf-8")],
    )

    # Everything past here runs under pythonw with no stderr, so an uncaught
    # exception would kill the app leaving nothing but a task exit code. Log it.
    try:
        log.info("waverider %s starting", wr.__version__)
        set_dpi_awareness()

        app = App()
        app.start_bridge()

        app.main_window = webview.create_window(
            "Waverider", app.cfg["wavelog_url"], width=1280, height=860)
        app.main_window.events.closing += app.on_main_closing
        app.main_window.events.shown += lambda: set_window_icon(app.main_window)

        # "Tools", not "Waverider": the window title already says that, and a
        # menu that repeats the line above it carries no information.
        menu = [Menu("Tools", [
            MenuAction("Setup", app.open_prefs),
            MenuAction("Open Wavelog in Browser", app.open_in_browser),
            MenuSeparator(),
            MenuAction("Quit", app.quit),
        ])]

        # devtools:true in config.json enables right-click Inspect in both
        # windows, which is the only way to see why WebView2 rejects a form the
        # server never hears about.
        debug = bool(app.cfg.get("devtools"))
        if debug:
            log.info("devtools enabled - right-click a window and choose Inspect")

        # private_mode=False + storage_path keeps the Wavelog login across runs.
        webview.start(menu=menu, private_mode=False,
                      storage_path=STORAGE_PATH, debug=debug)
    except Exception:
        log.exception("Fatal error - app is exiting")
        raise


if __name__ == "__main__":
    main()
