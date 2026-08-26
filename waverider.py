#!/usr/bin/env python3
"""
waverider.py - bridge rig CAT state to Wavelog's API v2.

Two radio backends behind make_bridge(). "flex" speaks the FlexRadio TCP API
natively: it subscribes to slice and transmit status and POSTs each in-use
slice to Wavelog as a separately named radio - Wavelog upserts on (radio,
operator, user_id), so each slice letter becomes its own entry, which is what
makes SO2R work without special handling. "rigctld" polls a hamlib daemon
instead and covers every rig hamlib speaks, one rig, one entry.

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
import re
import socket
import struct
import sys
import threading
import time
import urllib.error
import urllib.request

__version__ = "0.5.2"

HERE = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(HERE, "config.json")
LOG_PATH = os.path.join(HERE, "waverider.log")
TOKEN_PREFIX = "wl2_"

DEFAULT_CONFIG = {
    # Example values - config.json is written from these on first run and is
    # expected to be edited (or filled in from the app's Preferences window).
    #
    # Which CAT backend feeds Wavelog. "flex" speaks the FlexRadio TCP API
    # natively (slices, SO2R TX-follow, PGXL state). "rigctld" polls a hamlib
    # rigctld daemon instead - one rig, one Wavelog entry, but it covers every
    # rig hamlib speaks. Run rigctld yourself (it ships with hamlib and with
    # WSJT-X) and point these at it.
    "radio_backend": "flex",
    "rigctld_host": "127.0.0.1",
    "rigctld_port": 4532,
    "rigctld_radio_name": "Rig",
    "rigctld_poll_seconds": 1.0,
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
    # WSJT-X QSO forwarding: listen for the "Logged ADIF" UDP broadcast and
    # log each QSO into Wavelog. Onward sync (QRZ etc.) is Wavelog's job -
    # API-submitted QSOs skip the real-time QRZ push and ride the cron sync
    # with everything else.
    "wsjtx_enabled": True,
    "wsjtx_bind": "127.0.0.1",
    "wsjtx_port": 2237,
    # Desktop shell only: reload the main window after a QSO is delivered, so
    # the log view updates without a manual refresh. Only fires while the
    # window is on a read-only view (dashboard/logbook) - never mid-form.
    "auto_refresh_log": True,
    # The Wavelog station profile QSOs are filed under - the number in the URL
    # when editing the profile. The API rejects profiles the token doesn't own.
    "station_profile_id": 1,
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

# Hamlib's mode vocabulary, where it differs from what Wavelog understands.
# Same policy as MODE_MAP: data-on-sideband logs as the sideband, digital QSOs
# get their true mode from the WSJT-X logging path.
RIGCTLD_MODE_MAP = {
    "PKTUSB": "USB",
    "PKTLSB": "LSB",
    "PKTFM": "FM",
    "CWR": "CW",
    "RTTYR": "RTTY",
}

log = logging.getLogger("waverider")


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


# -- WSJT-X QSO forwarding ---------------------------------------------------

WSJTX_MAGIC = 0xADBCCBDA
WSJTX_LOGGED_ADIF = 12
SPOOL_PATH = os.path.join(HERE, "qso_spool.json")


def parse_wsjtx(datagram):
    """Return (msg_type, instance_id, adif_or_None) for a WSJT-X datagram.

    WSJT-X serialises with QDataStream: everything big-endian, and each utf8
    field is a quint32 byte count followed by the bytes (0xFFFFFFFF meaning
    null). Every message starts magic / schema / type / id. The id is the
    instance name ("WSJT-X - S1"), which is how a multi-instance station can
    be told apart on one socket. Type 12 (Logged ADIF) additionally carries
    the QSO as a complete ADIF document, which beats reassembling one from
    the 20-odd typed fields of the QSO Logged message.

    Returns None for anything that is not a WSJT-X datagram.
    """
    if len(datagram) < 16:
        return None
    magic, _schema, mtype = struct.unpack_from(">III", datagram, 0)
    if magic != WSJTX_MAGIC:
        return None
    offset = 12
    instance = ""
    adif = None
    try:
        (count,) = struct.unpack_from(">I", datagram, offset)
        offset += 4
        if count != 0xFFFFFFFF:
            instance = datagram[offset:offset + count].decode("utf-8", "replace")
            offset += count
        if mtype == WSJTX_LOGGED_ADIF:
            (count,) = struct.unpack_from(">I", datagram, offset)
            offset += 4
            if count != 0xFFFFFFFF:
                adif = datagram[offset:offset + count].decode("utf-8", "replace")
    except struct.error:
        return None
    return (mtype, instance, adif)


def adif_record(document):
    """The bare record from an ADIF document (WSJT-X sends header + <eoh>)."""
    record = re.split(r"<eoh>", document, flags=re.IGNORECASE)[-1].strip()
    return record if re.search(r"<eor>", record, re.IGNORECASE) else None


def adif_field(record, name):
    m = re.search(rf"<{name}:(\d+)(?::[^>]*)?>", record, re.IGNORECASE)
    if not m:
        return None
    length = int(m.group(1))
    return record[m.end():m.end() + length].strip() or None


class QsoForwarder:
    """Delivers logged QSOs to Wavelog, surviving server outages.

    Pending entries persist in qso_spool.json so a crash between log and
    delivery cannot lose a contact - the one kind of loss this file exists to
    prevent. Onward replication to QRZ and friends is Wavelog's cron sync's
    job, not ours.
    """

    RETRY_SECONDS = 60

    def __init__(self, cfg, dry_run=False):
        self.cfg = cfg
        self.dry_run = dry_run
        self.qso_url = cfg["wavelog_url"].rstrip("/") + "/api/v2/qso"
        self.lock = threading.Lock()
        self.wake = threading.Event()
        self.stop_requested = False
        self.warned = set()
        self.status = {"last_call": None, "last_at": None, "delivered": 0, "pending": 0}
        # Optional callable(count) invoked after a flush delivers QSOs. The
        # desktop shell hooks this to refresh the log view; headless runs
        # leave it None. Called on the forwarder thread.
        self.on_delivered = None
        self.pending = self._load_spool()
        self.status["pending"] = len(self.pending)

    def _load_spool(self):
        try:
            with open(SPOOL_PATH, encoding="utf-8") as fh:
                entries = json.load(fh)
            if entries:
                log.info("Spool holds %d undelivered QSO(s) from a previous run", len(entries))
            return entries
        except FileNotFoundError:
            return []
        except (json.JSONDecodeError, OSError) as err:
            log.error("Could not read %s (%s) - starting empty", SPOOL_PATH, err)
            return []

    def _save_spool(self):
        # Written on every change rather than at exit: the process dying is
        # exactly the case the spool is for.
        try:
            with open(SPOOL_PATH, "w", encoding="utf-8") as fh:
                json.dump(self.pending, fh, indent=2)
        except OSError as err:
            log.error("Could not write %s: %s", SPOOL_PATH, err)

    def submit(self, document):
        record = adif_record(document)
        if not record:
            log.warning("WSJT-X datagram had no ADIF record; ignored")
            return
        call = adif_field(record, "call") or "?"
        entry = {
            "record": record,
            "logged_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        }
        with self.lock:
            self.pending.append(entry)
            self._save_spool()
        log.info("QSO logged: %s - forwarding", call)
        self.status["last_call"] = call
        self.status["last_at"] = time.time()
        self.wake.set()

    # -- delivery ---------------------------------------------------------

    def _post_wavelog(self, record):
        if self.dry_run:
            log.info("DRY-RUN would POST QSO to Wavelog: %s", record)
            return True
        body = json.dumps({
            "station_profile_id": int(self.cfg["station_profile_id"]),
            "import_type": "adif",
            "adif": record,
        }).encode("utf-8")
        req = urllib.request.Request(
            self.qso_url, data=body, method="POST",
            headers={
                "Content-Type": "application/json",
                "Authorization": "Bearer {}".format(self.cfg["api_token"]),
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                summary = json.loads(resp.read().decode("utf-8", "replace"))
        except urllib.error.HTTPError as err:
            self._report_once("wavelog", err)
            return False
        except (urllib.error.URLError, json.JSONDecodeError) as err:
            log.warning("Wavelog QSO POST failed: %s", getattr(err, "reason", err))
            return False
        if summary.get("skipped"):
            # Already in the log - a WSJT-X retransmit or a spool replay.
            log.info("Wavelog skipped the QSO as a duplicate")
        return True

    def _report_once(self, target, err):
        if (target, err.code) in self.warned:
            return
        self.warned.add((target, err.code))
        detail = ""
        try:
            payload = json.loads(err.read().decode("utf-8", "replace"))
            error = payload.get("error", {})
            detail = "{}: {}".format(error.get("code", ""), error.get("message", "")).strip(": ")
        except Exception:
            pass
        log.error("%s QSO POST HTTP %s - %s", target, err.code, detail or err.reason)

    def flush(self):
        with self.lock:
            entries = list(self.pending)
        done = [e for e in entries if self._post_wavelog(e["record"])]
        with self.lock:
            self.pending = [e for e in self.pending if e not in done]
            if done:
                self._save_spool()
            self.status["delivered"] += len(done)
            self.status["pending"] = len(self.pending)
        if done:
            log.info("Delivered %d QSO(s); %d pending", len(done), len(self.pending))
            if self.on_delivered is not None:
                try:
                    self.on_delivered(len(done))
                except Exception as err:
                    log.warning("on_delivered hook failed: %s", err)

    def run(self):
        """Retry loop - flushes on submit() and every RETRY_SECONDS otherwise."""
        while not self.stop_requested:
            if self.pending:
                self.flush()
            self.wake.wait(self.RETRY_SECONDS)
            self.wake.clear()

    def stop(self):
        self.stop_requested = True
        self.wake.set()


class WsjtxListener(threading.Thread):
    """Owns the UDP socket WSJT-X broadcasts to, feeding the forwarder.

    Runs as a daemon so it can never hold the process open; orderly shutdown
    is stop() closing the socket out from under recvfrom.
    """

    def __init__(self, cfg, forwarder):
        super().__init__(daemon=True, name="wsjtx-listener")
        self.cfg = cfg
        self.forwarder = forwarder
        self.sock = None
        self.stop_requested = False
        # instances: {"WSJT-X - S1": unix_time_last_heard, ...} - every WSJT-X
        # message carries the instance name, so any traffic at all (heartbeats
        # every ~15s included) proves that instance's datagrams reach us. This
        # exists because "QSOs arrive" alone cannot distinguish "both instances
        # work" from "one works and the other has been silent".
        self.status = {"listening": False, "error": None, "instances": {}}

    def run(self):
        addr = (self.cfg["wsjtx_bind"], int(self.cfg["wsjtx_port"]))
        while not self.stop_requested:
            try:
                self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                self.sock.bind(addr)
            except OSError as err:
                # Usually another WSJT-X consumer holding the port. Keep
                # trying: the whole point is that QSOs must not be lost to a
                # transient squatter.
                self.status.update(listening=False, error=str(err))
                log.warning("Cannot bind UDP %s:%s (%s); retrying in 30s", *addr, err)
                time.sleep(30)
                continue
            self.status.update(listening=True, error=None)
            log.info("Listening for WSJT-X on %s:%s", *addr)
            while not self.stop_requested:
                try:
                    datagram, _ = self.sock.recvfrom(65536)
                except OSError:
                    break       # closed by stop(), or a stack-level failure
                parsed = parse_wsjtx(datagram)
                if parsed is None:
                    continue
                mtype, instance, adif = parsed
                if instance:
                    if instance not in self.status["instances"]:
                        log.info("WSJT-X instance heard: %s", instance)
                    self.status["instances"][instance] = time.time()
                if mtype == WSJTX_LOGGED_ADIF and adif:
                    self.forwarder.submit(adif)
            try:
                self.sock.close()
            except Exception:
                pass
        self.status["listening"] = False

    def stop(self):
        self.stop_requested = True
        if self.sock is not None:
            try:
                self.sock.close()
            except Exception:
                pass


class BridgeCore:
    """What every radio backend shares: throttled publishing into Wavelog.

    A backend subclasses this, produces {radio name: payload} dicts however its
    rig's protocol works, and hands them to publish_payloads() - which owns the
    change detection, the heartbeat, the rate floor, and the status the GUI
    renders. Backends differ only in where payloads come from.
    """

    def __init__(self, cfg, dry_run=False):
        self.cfg = cfg
        self.wavelog = WavelogClient(cfg, dry_run=dry_run)
        self.last_sent = {}     # radio name -> (payload, monotonic timestamp)
        # Read by the GUI to render live status; harmless when running headless.
        self.status = {"connected": False, "error": None, "radios": {}, "amp": None}
        self.stop_requested = False

    def publish_payloads(self, payload_map):
        now = time.monotonic()
        heartbeat = self.cfg["heartbeat_seconds"]
        floor = self.cfg["min_post_interval"]
        for name, payload in payload_map.items():
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


class FlexBridge(BridgeCore):
    def __init__(self, cfg, dry_run=False):
        super().__init__(cfg, dry_run=dry_run)
        self.slices = {}        # slice index -> merged attribute dict
        self.amplifiers = {}    # amp handle -> merged attribute dict
        self.rfpower = None

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
        state = self.amp_state()
        if state != self.status["amp"]:
            log.info("Amplifier state: %s", state or "not present")
        self.status["amp"] = state
        self.publish_payloads(self.payloads())

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


class RigctldBridge(BridgeCore):
    """CAT via a hamlib rigctld daemon - the brand-agnostic backend.

    Covers every rig hamlib speaks (Elecraft, Icom, Yaesu, Kenwood, ...) at the
    cost of the Flex-only niceties: one rig, one Wavelog entry, no slices, no
    TX-follow, no amplifier state, no power. Frequency and mode are polled on
    an interval because the rigctld protocol has no push.

    Protocol notes (default mode, not extended): commands are single letters
    terminated by newline. 'f' answers one line, frequency in Hz. 'm' answers
    two lines, mode name then passband width. A command the rig cannot serve
    answers a single 'RPRT <negative>' line instead.
    """

    def __init__(self, cfg, dry_run=False):
        super().__init__(cfg, dry_run=dry_run)
        self.sock = None
        self.reader = None

    def connect(self):
        host, port = self.cfg["rigctld_host"], int(self.cfg["rigctld_port"])
        log.info("Connecting to rigctld at %s:%s", host, port)
        self.sock = socket.create_connection((host, port), timeout=5)
        self.sock.settimeout(5.0)
        self.reader = self.sock.makefile("r", encoding="ascii", errors="replace")
        self.last_sent.clear()
        self.status.update(connected=True, error=None)

    def query(self, cmd, lines):
        """Send one command, return its reply lines, or None on a rig error."""
        self.sock.sendall((cmd + "\n").encode("ascii"))
        out = []
        for _ in range(lines):
            line = self.reader.readline()
            if not line:
                raise ConnectionError("rigctld closed the connection")
            line = line.strip()
            if line.startswith("RPRT"):
                return None
            out.append(line)
        return out

    def poll_payload(self):
        reply = self.query("f", 1)
        if not reply:
            return None
        try:
            hz = int(float(reply[0]))
        except ValueError:
            return None
        payload = {"radio": self.cfg["rigctld_radio_name"], "frequency": hz}
        reply = self.query("m", 2)
        if reply and reply[0]:
            mode = reply[0].upper()
            payload["mode"] = RIGCTLD_MODE_MAP.get(mode, mode)
        return payload

    def run(self):
        backoff = 1
        interval = float(self.cfg["rigctld_poll_seconds"])
        while not self.stop_requested:
            try:
                self.connect()
                backoff = 1
                log.info("Connected. Publishing as %r", self.cfg["rigctld_radio_name"])
                while not self.stop_requested:
                    payload = self.poll_payload()
                    if payload:
                        self.publish_payloads({payload["radio"]: payload})
                    time.sleep(interval)
            except (OSError, ConnectionError) as err:
                self.status.update(connected=False, error=str(err))
                log.warning("Lost rigctld (%s); retrying in %ss", err, backoff)
                try:
                    self.sock.close()
                except Exception:
                    pass
                time.sleep(backoff)
                backoff = min(backoff * 2, 30)
        self.status["connected"] = False


BACKENDS = {"flex": FlexBridge, "rigctld": RigctldBridge}


def make_bridge(cfg, dry_run=False):
    backend = (cfg.get("radio_backend") or "flex").lower()
    cls = BACKENDS.get(backend)
    if cls is None:
        log.error("Unknown radio_backend %r - choices: %s",
                  backend, ", ".join(sorted(BACKENDS)))
        sys.exit(1)
    return cls(cfg, dry_run=dry_run)


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
    bridge = make_bridge(cfg, dry_run=args.dry_run)
    forwarder = listener = None
    if cfg.get("wsjtx_enabled"):
        forwarder = QsoForwarder(cfg, dry_run=args.dry_run)
        threading.Thread(target=forwarder.run, daemon=True,
                         name="qso-forwarder").start()
        listener = WsjtxListener(cfg, forwarder)
        listener.start()
    try:
        bridge.run()
    except KeyboardInterrupt:
        log.info("Stopped.")
    finally:
        if listener is not None:
            listener.stop()
        if forwarder is not None:
            forwarder.stop()


if __name__ == "__main__":
    main()
