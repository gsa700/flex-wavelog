#!/usr/bin/env python3
"""
app.py - desktop shell for Wavelog with the FlexRadio CAT bridge built in.

A single window hosting the Wavelog web UI, a preferences/status window reached
from the native menu, and FlexBridge running on a background thread so CAT
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

import webview
from webview.menu import Menu, MenuAction, MenuSeparator

import flex_wavelog as fw

HERE = os.path.dirname(os.path.abspath(__file__))
PREFS_HTML = os.path.join(HERE, "ui", "prefs.html")
# WebView2 profile. Without a persistent store the Wavelog session is thrown
# away on exit and you log in again on every launch.
STORAGE_PATH = os.path.join(HERE, ".webview")

log = logging.getLogger("flex-wavelog.app")


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
        return cfg

    def save_config(self, incoming):
        cfg = dict(self._app.cfg)

        for key in ("flex_host", "wavelog_url"):
            value = (incoming.get(key) or "").strip()
            if not value:
                return {"ok": False, "error": f"{key} cannot be empty"}
            cfg[key] = value

        # An empty tx_radio_name is meaningful: it disables the follower entry.
        tx_name = (incoming.get("tx_radio_name") or "").strip()
        cfg["tx_radio_name"] = tx_name or None

        try:
            cfg["flex_port"] = int(incoming.get("flex_port") or 4992)
            cfg["heartbeat_seconds"] = float(incoming.get("heartbeat_seconds") or 5)
            cfg["min_post_interval"] = float(incoming.get("min_post_interval") or 1)
            cfg["amp_power_watts"] = int(incoming.get("amp_power_watts") or 0)
        except (TypeError, ValueError):
            return {"ok": False, "error": "Port and interval fields must be numbers"}

        cfg["send_power"] = bool(incoming.get("send_power"))

        names = {}
        for letter in ("A", "B"):
            value = (incoming.get(f"radio_{letter}") or "").strip()
            if value:
                names[letter] = value
        cfg["radio_names"] = names

        token = (incoming.get("api_token") or "").strip()
        if token:
            if not token.startswith(fw.TOKEN_PREFIX):
                return {"ok": False,
                        "error": f"Token must start with '{fw.TOKEN_PREFIX}' - "
                                 "a v1 key won't work against /api/v2/radio"}
            cfg["api_token"] = token
        if not cfg.get("api_token") or "PASTE_YOUR" in cfg["api_token"]:
            return {"ok": False, "error": "An API token is required"}

        # Strip the display-only keys before they reach disk.
        for key in ("token_is_set", "radio_A", "radio_B"):
            cfg.pop(key, None)

        with open(fw.CONFIG_PATH, "w", encoding="utf-8") as fh:
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
        return {
            "connected": bridge.status["connected"],
            "error": bridge.status["error"],
            "amp": bridge.status.get("amp"),
            "radios": radios,
        }

    def quit(self):
        self._app.quit()


class App:
    def __init__(self):
        self.cfg = fw.load_config()
        self.api = Api(self)
        self.bridge = None
        self.thread = None
        self.main_window = None
        self.prefs_window = None
        self.quitting = False

    # -- bridge lifecycle -------------------------------------------------

    def start_bridge(self):
        self.bridge = fw.FlexBridge(self.cfg)
        self.thread = threading.Thread(target=self.bridge.run, daemon=True,
                                       name="flex-bridge")
        self.thread.start()

    def stop_bridge(self):
        if self.bridge is not None:
            self.bridge.stop_requested = True
            # The reader sits in recv with a 1s timeout; closing the socket drops
            # it out immediately rather than waiting for the loop to notice.
            try:
                self.bridge.sock.close()
            except Exception:
                pass
        if self.thread is not None:
            self.thread.join(timeout=5)
        self.bridge = None
        self.thread = None

    def restart_bridge(self):
        self.stop_bridge()
        self.start_bridge()

    # -- windows ----------------------------------------------------------

    def open_prefs(self):
        if self.prefs_window is None:
            self.prefs_window = webview.create_window(
                "Preferences and Status", PREFS_HTML,
                js_api=self.api, width=820, height=760)
            self.prefs_window.events.closed += self._prefs_closed
        else:
            self.prefs_window.show()

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
            fw.LOG_PATH, maxBytes=1_000_000, backupCount=3, encoding="utf-8")],
    )

    set_dpi_awareness()

    app = App()
    app.start_bridge()

    app.main_window = webview.create_window(
        "Wavelog", app.cfg["wavelog_url"], width=1280, height=860)
    app.main_window.events.closing += app.on_main_closing

    menu = [Menu("Flex-Wavelog", [
        MenuAction("Preferences and Status", app.open_prefs),
        MenuSeparator(),
        MenuAction("Quit", app.quit),
    ])]

    # private_mode=False + a storage_path keeps the Wavelog login across runs.
    webview.start(menu=menu, private_mode=False, storage_path=STORAGE_PATH)


if __name__ == "__main__":
    main()
