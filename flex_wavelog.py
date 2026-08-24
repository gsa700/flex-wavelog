#!/usr/bin/env python3
"""
flex_wavelog.py - bridge FlexRadio slice state to Wavelog's API v2.

Connects to the radio's TCP API, subscribes to slice and transmit status, and
POSTs each in-use slice to Wavelog as a separately named radio. Because Wavelog
upserts on (radio, operator, user_id), each slice letter becomes its own radio
entry - which is what makes SO2R work without any special handling.

Targets POST /api/v2/radio with a Bearer token ("wl2_" prefix, radio:write
scope). The legacy v1 /api/radio endpoint would also work, but v1 keys are
stored in the database in plaintext whereas v2 tokens are hashed and scoped.

Stdlib only. Config lives in config.json next to this file.
"""

import argparse
import json
import logging
import logging.handlers
import os
import socket
import sys
import time
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(HERE, "config.json")
LOG_PATH = os.path.join(HERE, "flex_wavelog.log")
TOKEN_PREFIX = "wl2_"

DEFAULT_CONFIG = {
    # Example values - config.json is written from these on first run and is
    # expected to be edited (or filled in from the app's Preferences window).
    "flex_host": "192.168.1.50",
    "flex_port": 4992,
    "wavelog_url": "http://192.168.1.10",
    "api_token": "PASTE_YOUR_WL2_TOKEN_HERE",
    # Slice index_letter -> the radio name Wavelog will show. Add C/D if you
    # ever run more slices; unmapped letters are ignored.
    "radio_names": {"A": "Radio A", "B": "Radio B"},
    # A virtual radio that always mirrors whichever slice currently holds tx=1.
    # Select this one in Wavelog and you never re-pick when the transmitter
    # moves between slices. Set to null to disable.
    "tx_radio_name": "Radio TX",
    # Wavelog marks a radio stale if its timestamp stops moving, so we resend
    # unchanged state on this interval.
    "heartbeat_seconds": 5,
    # Floor between POSTs for a single radio, so fast tuning can't hammer the API.
    "min_post_interval": 1.0,
    "send_power": True,
    # Nominal output to log while the amplifier is out of standby. Drive power
    # from the radio is used whenever it is in standby.
    "amp_power_watts": 1000,
}

# Flex reports rig modes; Wavelog's MODE_OVERRIDES doesn't know the DIG* ones and
# passes unknown values through verbatim. It does map USB-D -> USB, so data-on-
# sideband is meant to be plain USB/LSB. Digital QSOs get their real mode (FT8,
# etc.) from WSJT-X's logging path, not from CAT.
MODE_MAP = {
    "DIGU": "USB",
    "DIGL": "LSB",
    "SAM": "AM",
    "NFM": "FM",
    "DFM": "FM",
}

log = logging.getLogger("flex-wavelog")


def load_config(require_key=True):
    if not os.path.exists(CONFIG_PATH):
        with open(CONFIG_PATH, "w", encoding="utf-8") as fh:
            json.dump(DEFAULT_CONFIG, fh, indent=2)
        log.error("Wrote a starter config to %s - add your API key and rerun.", CONFIG_PATH)
        sys.exit(1)
    # utf-8-sig, not utf-8: a UTF-8 BOM is invisible in every editor and makes
    # json.load fail at char 0 with a message that points nowhere useful. Windows
    # tools write one readily - Notepad, and PowerShell 5.1's -Encoding utf8.
    with open(CONFIG_PATH, encoding="utf-8-sig") as fh:
        cfg = json.load(fh)
    for key, val in DEFAULT_CONFIG.items():
        cfg.setdefault(key, val)
    if require_key:
        token = cfg["api_token"]
        if "PASTE_YOUR" in token:
            log.error("api_token in %s is still the placeholder.", CONFIG_PATH)
            sys.exit(1)
        if not token.startswith(TOKEN_PREFIX):
            # The dispatcher fast-fails non-v2 credentials; catching it here
            # gives a message that says what to do about it.
            log.error("api_token does not start with %r - that looks like a legacy v1 key. "
                      "/api/v2/radio only accepts v2 tokens with the radio:write scope.",
                      TOKEN_PREFIX)
            sys.exit(1)
    return cfg


def parse_kv(text):
    """Split 'a=1 b=2' into a dict, ignoring bare tokens."""
    out = {}
    for tok in text.split():
        if "=" in tok:
            k, v = tok.split("=", 1)
            out[k] = v
    return out


class WavelogClient:
    def __init__(self, cfg, dry_run=False):
        self.url = cfg["wavelog_url"].rstrip("/") + "/api/v2/radio"
        self.token = cfg["api_token"]
        self.dry_run = dry_run
        self.warned = set()

    def post(self, payload):
        if self.dry_run:
            log.info("DRY-RUN would POST %s", json.dumps(payload, sort_keys=True))
            return True
        body = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            self.url, data=body, method="POST",
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self.token}",
            },
        )
        try:
            # 201 when the radio name is new, 200 on every subsequent update.
            with urllib.request.urlopen(req, timeout=10) as resp:
                resp.read()
            return True
        except urllib.error.HTTPError as err:
            self.report(err)
            return False
        except urllib.error.URLError as err:
            log.warning("Wavelog unreachable: %s", err.reason)
            return False

    def report(self, err):
        """Surface the v2 error body, once per status code.

        Auth and scope failures are configuration problems, not transient ones,
        so repeating them every heartbeat would only bury the useful line.
        """
        if err.code == 429:
            log.warning("Wavelog rate-limited us; backing off")
            return
        if err.code in self.warned:
            return
        self.warned.add(err.code)
        detail = ""
        try:
            payload = json.loads(err.read().decode("utf-8", "replace"))
            error = payload.get("error", {})
            detail = "{}: {}".format(error.get("code", ""), error.get("message", "")).strip(": ")
        except Exception:
            pass
        log.error("Wavelog HTTP %s - %s", err.code, detail or err.reason)


class FlexBridge:
    def __init__(self, cfg, dry_run=False):
        self.cfg = cfg
        self.wavelog = WavelogClient(cfg, dry_run=dry_run)
        self.slices = {}        # slice index -> merged attribute dict
        self.amplifiers = {}    # amp handle -> merged attribute dict
        self.rfpower = None
        self.last_sent = {}     # radio name -> (payload, monotonic timestamp)
        # Read by the GUI to render live status; harmless when running headless.
        self.status = {"connected": False, "error": None, "radios": {}, "amp": None}
        self.stop_requested = False

    # -- Flex protocol ----------------------------------------------------

    def connect(self):
        host, port = self.cfg["flex_host"], self.cfg["flex_port"]
        log.info("Connecting to FlexRadio at %s:%s", host, port)
        sock = socket.create_connection((host, port), timeout=10)
        sock.settimeout(1.0)
        self.sock = sock
        self.buf = b""
        self.slices.clear()
        self.last_sent.clear()
        seq = 1
        for cmd in ("sub slice all", "sub tx all", "sub amplifier all"):
            sock.sendall(f"C{seq}|{cmd}\n".encode())
            seq += 1
        self.status["connected"] = True
        self.status["error"] = None

    def read_lines(self):
        """Yield complete lines, or nothing if the socket is just idle."""
        try:
            chunk = self.sock.recv(8192)
        except socket.timeout:
            return
        if not chunk:
            raise ConnectionError("radio closed the connection")
        self.buf += chunk
        while b"\n" in self.buf:
            line, self.buf = self.buf.split(b"\n", 1)
            yield line.decode("utf-8", "replace").strip()

    def handle(self, line):
        if not line.startswith("S") or "|" not in line:
            return
        _, _, body = line.partition("|")
        if body.startswith("slice "):
            rest = body[len("slice "):]
            idx, _, attrs = rest.partition(" ")
            # Slice updates are incremental - a retune sends RF_frequency alone,
            # so state has to be merged rather than replaced.
            self.slices.setdefault(idx, {}).update(parse_kv(attrs))
        elif body.startswith("transmit "):
            tx = parse_kv(body[len("transmit "):])
            if "rfpower" in tx:
                self.rfpower = tx["rfpower"]
        elif body.startswith("amplifier "):
            rest = body[len("amplifier "):]
            handle_id, _, attrs = rest.partition(" ")
            # Incremental like slices - a state change arrives on its own.
            self.amplifiers.setdefault(handle_id, {}).update(parse_kv(attrs))

    # -- Wavelog side -----------------------------------------------------

    def build(self, attrs, name):
        freq = attrs.get("RF_frequency")
        if not freq:
            return None
        payload = {"radio": name}
        # Flex reports MHz as a decimal string; Wavelog's cat.frequency is Hz.
        payload["frequency"] = int(round(float(freq) * 1_000_000))
        mode = attrs.get("mode")
        if mode:
            payload["mode"] = MODE_MAP.get(mode.upper(), mode.upper())
        # Power belongs to whichever slice is actually transmitting - which is
        # always true for the tx-follower entry, and true for A or B only while
        # that slice holds the transmitter.
        if self.cfg["send_power"] and attrs.get("tx") == "1":
            watts = self.tx_power_watts()
            if watts:
                payload["power"] = watts
        return payload

    def amp_state(self):
        """Current state string of the Power Genius, or None if there isn't one.

        The Flex publishes each amplifier as a control object. The Tuner Genius
        appears here too, so match on model rather than taking the first entry.
        """
        for attrs in self.amplifiers.values():
            if "PowerGenius" in (attrs.get("model") or ""):
                return (attrs.get("state") or "").upper() or None
        return None

    def tx_power_watts(self):
        """Nominal power to log.

        ADIF TX_PWR is a nominal figure, not an instantaneous sample - reading
        forward power at the moment of save would capture wherever the envelope
        happened to be, which on SSB is close to meaningless. So: the amp's
        configured output when the PGXL is in line, the radio's drive power when
        it is in standby.

        Observed states are STANDBY (out of line) and IDLE (in line, not keyed).
        The test is deliberately negative - only STANDBY counts as out of line -
        because the failure modes are not symmetric. Treating an unknown state as
        in-line over-reports drive power as amp power; testing positively for a
        state name would silently log a kilowatt as 33 W the first time the
        firmware used a word this code had not seen. IDLE was in fact the first
        such surprise.
        """
        state = self.amp_state()
        if state is not None and state != "STANDBY":
            try:
                return int(float(self.cfg.get("amp_power_watts") or 0)) or None
            except (TypeError, ValueError):
                return None
        if self.rfpower:
            try:
                return int(float(self.rfpower)) or None
            except ValueError:
                return None
        return None

    def payloads(self):
        """Every radio entry this tick should consider, keyed by radio name."""
        out = {}
        tx_attrs = None
        for attrs in self.slices.values():
            if attrs.get("in_use") != "1":
                continue
            if attrs.get("tx") == "1":
                tx_attrs = attrs
            name = self.cfg["radio_names"].get(attrs.get("index_letter"))
            if name:
                payload = self.build(attrs, name)
                if payload:
                    out[name] = payload
        # The tx-follower rides on top of the per-slice entries; if nothing is
        # transmitting we simply leave its last value standing in Wavelog.
        tx_name = self.cfg.get("tx_radio_name")
        if tx_name and tx_attrs is not None:
            payload = self.build(tx_attrs, tx_name)
            if payload:
                out[tx_name] = payload
        return out

    def publish(self):
        now = time.monotonic()
        heartbeat = self.cfg["heartbeat_seconds"]
        floor = self.cfg["min_post_interval"]
        state = self.amp_state()
        if state != self.status["amp"]:
            log.info("Amplifier state: %s", state or "not present")
        self.status["amp"] = state
        for name, payload in self.payloads().items():
            prev, sent_at = self.last_sent.get(name, (None, 0.0))
            changed = payload != prev
            due = (now - sent_at) >= heartbeat
            if not (changed or due):
                continue
            if changed and (now - sent_at) < floor:
                continue    # tuning fast; let it settle
            if self.wavelog.post(payload):
                if changed:
                    log.info("%s -> %.6f MHz %s", name,
                             payload["frequency"] / 1_000_000,
                             payload.get("mode", "?"))
                self.last_sent[name] = (payload, now)
                self.status["radios"][name] = {
                    "frequency": payload["frequency"],
                    "mode": payload.get("mode"),
                    "power": payload.get("power"),
                    "updated": time.time(),
                }

    # -- main loop --------------------------------------------------------

    def run(self):
        backoff = 1
        while not self.stop_requested:
            try:
                self.connect()
                backoff = 1
                log.info("Connected. Watching slices: %s",
                         ", ".join(f"{k}={v}" for k, v in self.cfg["radio_names"].items()))
                while not self.stop_requested:
                    for line in self.read_lines():
                        self.handle(line)
                    self.publish()
            except (OSError, ConnectionError) as err:
                self.status["connected"] = False
                self.status["error"] = str(err)
                log.warning("Lost radio (%s); retrying in %ss", err, backoff)
                try:
                    self.sock.close()
                except Exception:
                    pass
                time.sleep(backoff)
                backoff = min(backoff * 2, 30)
        self.status["connected"] = False


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dry-run", action="store_true",
                    help="log the payloads instead of POSTing; no API key needed")
    args = ap.parse_args()

    # Under pythonw.exe (how the scheduled task runs it) there is no console and
    # sys.stderr is None, so the file handler is the only way to see anything.
    handlers = [logging.handlers.RotatingFileHandler(
        LOG_PATH, maxBytes=1_000_000, backupCount=3, encoding="utf-8")]
    if sys.stderr is not None:
        handlers.append(logging.StreamHandler())
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        handlers=handlers,
    )
    cfg = load_config(require_key=not args.dry_run)
    bridge = FlexBridge(cfg, dry_run=args.dry_run)
    try:
        bridge.run()
    except KeyboardInterrupt:
        log.info("Stopped.")


if __name__ == "__main__":
    main()
