# flex-wavelog

Publishes FlexRadio slice state to [Wavelog](https://www.wavelog.org/) so your log's
frequency, mode and power follow the radio — with first-class SO2R handling.
Also logs every WSJT-X QSO straight into Wavelog as it happens.

Two ways to run it:

| | |
|---|---|
| `flex_wavelog.py` | Headless bridge. Standard library only, no dependencies. |
| `app.py` | Desktop shell — Wavelog in a window, with the bridge running inside it. Needs `pywebview`. |

## Why SO2R needs special handling

A Flex runs multiple slice receivers, and Wavelog's `/api/v2/radio` takes one
frequency per named radio. Publishing "the" frequency means picking one, and any
bridge that picks wrong is wrong about half the time — because `tx` and `active`
are independent. In a typical SO2R moment the transmitter sits on slice A while
the UI focus is on slice B.

Wavelog upserts radios on `(radio, operator, user_id)`, so the radio *name* is the
identity. This bridge exploits that by publishing three entries:

- **Radio A** / **Radio B** — one per slice, each with its own frequency and mode.
- **Radio TX** — a virtual radio mirroring whichever slice currently holds `tx=1`.

Select **Radio TX** in Wavelog's QSO entry and it follows your transmitter across
slices with no further interaction. You log on TX; so does the logger.

Power is only ever attached to the transmitting slice — reporting output power on a
slice you're merely listening on would put a falsehood in your log.

## WSJT-X QSO forwarding

WSJT-X broadcasts every logged QSO over UDP (default `127.0.0.1:2237`, under
*Settings → Reporting*). The bridge listens there, takes the ready-made ADIF
record from the **Logged ADIF** message, and logs it into Wavelog —
`POST /api/v2/qso` with `import_type: "adif"`, filed under
`station_profile_id`. The API token needs the `qso:write` scope in addition
to `radio:write`.

Onward replication is deliberately left to Wavelog: once the QSO is in the
main log, the server's cron sync carries it to QRZ (and anywhere else that's
configured) with the rest of the log. Note that Wavelog skips its *real-time*
QRZ push for API-submitted QSOs, so uploads land on the cron cadence, not
instantly — by design, one source of truth and one uploader.

Undelivered QSOs (server down, no network) persist in `qso_spool.json` and are
retried every minute; duplicates reported by the server on a replay are
treated as delivered.

## Setup

### Windows

```
.\install.ps1
```

A readable script, not a packaged binary — an unsigned PyInstaller .exe gets
flagged by antivirus on download reputation alone, which is exactly why small
ham utilities so often look untrustworthy. Read the script first; that's the
point of it. It checks Python 3.9+, installs dependencies, seeds `config.json`,
registers a logon-time scheduled task, and adds a Start Menu shortcut. Requires
no admin rights, safe to re-run, never overwrites an existing config.

`.\uninstall.ps1` removes all of it — including the scheduled task and the
`.webview` profile (which holds a live Wavelog session cookie). `config.json`
is kept unless you pass `-Purge`, because it contains your API token.

### Manual / other platforms

```
pip install -r requirements.txt          # only for app.py
cp config.example.json config.json       # then edit, or use the Preferences window
```

You need a Wavelog **API v2 token** (`wl2_` prefix) carrying the `radio:write`
scope, from Wavelog's *API Keys* page. Legacy v1 keys are rejected by
`/api/v2/radio`. Prefer v2 regardless: v1 keys sit in the database in plaintext,
v2 tokens are hashed, scoped and individually revocable.

```
python flex_wavelog.py --dry-run    # log payloads without POSTing; no token needed
python flex_wavelog.py              # headless
python app.py                       # desktop shell
```

`config.json` holds your token in cleartext and is gitignored. Keep it that way.

## Implementation notes

Things that cost real time to work out, recorded so they don't have to be again:

- **Wavelog's `cat.frequency` is Hz.** The `14075` in Wavelog's own `Api::radio()`
  comment looks like kHz and is misleading; `Cat::get_mode_designator()` compares
  against `21000000` / `28000000` / `144000000`. Flex reports MHz, so multiply.
- **`DIGU` / `DIGL` are not in Wavelog's `MODE_OVERRIDES`,** and unknown modes pass
  through verbatim — you'd store a literal `DIGU`, which is not an ADIF mode. The
  override table maps `USB-D` → `USB`, so data-on-sideband is meant to be plain
  `USB`/`LSB`. Digital QSOs get their real mode (FT8 etc.) from WSJT-X's logging
  path, not from CAT.
- **Slice status carries no `scu` field.** `index_letter` is the only sane way to
  tell slices apart.
- **Slice updates are incremental.** A retune sends `RF_frequency` alone, so slice
  state must be merged, not replaced.
- Wavelog marks a radio stale if its timestamp stops moving, hence the heartbeat
  resending unchanged state every few seconds.
- **Amplifier state comes free over the same connection.** `sub amplifier all`
  reports a Power Genius XL without needing its telnet credentials, so logged
  power can follow the amp: configured output when it is in line, radio drive
  power when it is in standby. Match on `model` — a Tuner Genius appears in the
  same list. Observed states are `STANDBY` and `IDLE`; test *negatively* against
  STANDBY rather than positively for an in-line state name, or an unrecognised
  state silently logs a kilowatt as drive power.
- Real forward power *is* available (`FWD`, `src=AMP`, in dBm), but meter values
  stream over UDP VITA-49 rather than the text API — and ADIF wants nominal
  power anyway, so an instantaneous sample would be worse, not better.
- **WSJT-X's UDP protocol is QDataStream**: big-endian, magic `0xadbccbda`,
  then schema, message type, and length-prefixed fields (`0xFFFFFFFF` = null).
  Message type 12 (*Logged ADIF*) carries the QSO as a complete ADIF document
  with an `<eoh>` header — far simpler than reassembling type 5's twenty typed
  fields, some of which are Qt `QDateTime` structures.
- **Wavelog skips its real-time QRZ push for API-submitted QSOs** (documented,
  for performance). QSOs logged through the API reach QRZ on the cron sync
  cadence instead — fine here, since the cron sync is how this station keeps
  QRZ current anyway.

## Desktop shell

`app.py` puts Wavelog in a window and runs the bridge on a background thread.
Closing the window **minimises** it — an X-click that silently stopped CAT
publishing mid-contest would be a poor surprise. Quit deliberately from the menu.

Preferences and live bridge status are on the menu too. Saving preferences rewrites
`config.json` and restarts the bridge in place. The token is stored but never
rendered back to the page.

## Licence

MIT.
