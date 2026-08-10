# Mitsubishi Heavy Industries WF-RAC WLAN module — external interface reference

**Audience:** anyone writing software that talks to an MHI WF-RAC WLAN module
over the network, and anyone building a CNS/SPI replacement (ESP32, ESP8266)
who wants to know what the stock module actually does on the bus.

**Purpose:** interoperability. This describes the interfaces a client needs in
order to talk to the module correctly - nothing here is a guide to modifying,
reflashing or circumventing anything in it, and the sections that touch write
paths say so explicitly.

**Direction of travel:** outside in. Network → HTTP → `airconStat` blob →
CNS/SPI frame. The module's own internals appear only where they explain a
behaviour you can observe from outside; the raw evidence is in
[Appendix A](#appendix-a--bridge-mcu-internals-provenance).

**Reading the confidence tags.** Every non-obvious claim carries one. Do not
silently upgrade them.

| Tag | Meaning |
| --- | --- |
| `[HW]` | Observed on real hardware (SRK35ZS-WF, firmware `WF-RAC-HTTPS 025/200`) |
| `[FW]` | Read out of a firmware image (module or bridge MCU), static analysis |
| `[APP]` | Read out of the official Smart M-Air app (1.4.009, unobfuscated) |
| `[EXT]` | External project documentation, chiefly [MHI-AC-Trace](https://github.com/absalom-muc/MHI-AC-Trace) / [MHI-AC-Ctrl](https://github.com/absalom-muc/MHI-AC-Ctrl) |
| `[INF]` | Inference from the above. Plausible, **not** tested |

---

## 1. What the module is

The WF-RAC module is **two processors**, not one:

```
        ┌──────────────────────── WF-RAC module ───────────────────────┐
 Wi-Fi  │  Realtek RTL8710C (Ameba-Z2)        Renesas RL78             │   CNS
════════╪══ FreeRTOS, mbedTLS 2.4.0  ══UART══ protocol bridge  ════════╪══════ AC
 HTTPS  │  HTTP/MQTT server, TLS,             airconStat ⇄ SPI frames  │  SPI   main
 :51443 │  base64, OTA, Wi-Fi                 (acts as the remote      │        board
        │  "M" firmware                        control) "C" firmware   │
        └──────────────────────────────────────────────────────────────┘
```

- The **Realtek** side (`M` firmware, e.g. `WCBN4612L_M_029.bin`) is network
  plumbing. It base64-decodes the `airconStat` blob and hands the raw bytes to
  the RL78. **It never interprets a single status bit.** `[FW]`
- The **RL78** side (`C` firmware, e.g. `WF-RAC-HTTPS_C_200`) is the
  interesting one: it is the **CNS/SPI remote control**, i.e. exactly the role
  an ESP32 running MHI-AC-Ctrl takes over when you unplug the module. It
  receives MOSI frames, sends MISO frames, and translates both directions into
  the `airconStat` blob. `[FW]`

**Consequence for ESP32 developers:** the WF-RAC network protocol is not a
separate protocol. It is the MHI CNS/SPI frame wrapped in base64 and JSON, with
a fixed set of bits stripped out. Everything MHI-AC-Trace documents about
`DB0…DB14` applies, with a known offset (§4).

**Consequence for network developers:** anything the AC's main board knows but
the CNS bus doesn't carry is unreachable, and anything the bridge chooses not to
forward is unreachable — but the request channel is generic and largely unused
by the official app (§5).

---

## 2. Layer 1 — discovery and the local HTTP API

### 2.1 Finding the device

- **mDNS service `_beaver._tcp`** `[FW]` `[HW]`
  - instance name: MAC address without colons, lower case (`348e89c5a137`)
  - host: `<mac>.local`
  - port: `51443`
  - TXT record: empty
- No UDP broadcast protocol, no discovery endpoint.

### 2.2 Transport

| Property | Value |
| --- | --- |
| Port | `51443` (all firmware branches) `[HW]` |
| Scheme | `https://` on the `WF-RAC-HTTPS` and `WCBN4612L` branches, plain `http://` on the older `WF-RAC` branch `[HW]` |
| Method | `POST` only |
| Path | `/beaver/command/<commandName>` |
| Body | JSON, see below |

**TLS is ancient.** The module links mbedTLS 2.4.0 (2016) `[FW]`. Modern clients
fail the handshake with `SSLV3_ALERT_HANDSHAKE_FAILURE` unless you drop the
security level and allow legacy renegotiation. The certificate is self-signed
and its CN does not match the IP, so hostname verification must be off. If you
do not know the branch, probe: try one scheme, fall back to the other.

Some modules answer with a **valid JSON body under the wrong `Content-Type`**
(`text/plain`) `[HW]`. Parse the body regardless of the declared type, and parse
it on non-2xx statuses too — error responses carry useful JSON.

### 2.3 Request envelope

Every request, without exception:

```json
{
  "apiVer": "1.0",
  "command": "getAirconStat",
  "deviceId": "<your client id, free-form string>",
  "operatorId": "<your operator UUID>",
  "timestamp": 1754400000,
  "contents": { "airconId": "348e89c5a137" }
}
```

**All five envelope fields are mandatory and validated before the command is
dispatched.** `[FW]` The parser reads them in this order and returns an error on
the first failure, so a missing envelope field looks exactly like a malformed
request:

| Field | JSON type | Checked how |
| --- | --- | --- |
| `command` | string | present, non-empty; then dispatched |
| `apiVer` | string | present, non-empty — **the value is never compared.** The literal `"1.0"` exists in the image only for *emitting* responses. Send anything non-empty and it passes |
| `operatorId` | string | present, non-empty here; `setAirconStat` additionally validates it against the stored accounts (§2.5) |
| `deviceId` | string | present, non-empty; logged (`deviceId_value=%s`) and passed on to the command handler |
| `timestamp` | number | present **and non-zero** |
| `contents` | object | required by every handler except `getDeviceInfo` |

**`timestamp` sets the module's clock.** `[FW]` The parsed 64-bit value is
written straight into the module's time global. The module has no RTC, so *your
client is its time source* — which is where account `expires` values and the
MQTT `aggTm` stamp come from. Send a real Unix timestamp in seconds; sending
garbage will skew the device's notion of time.

Responses carry `command`, `apiVer: "1.0"`, `result` and `contents`. `[FW]`

### 2.4 Endpoints

Seven handlers are registered in the module firmware; there are no others. `[FW]`

| Command | `contents` required | Notes |
| --- | --- | --- |
| `getDeviceInfo` | *(none — body is not evaluated at all)* `[FW]` | Always answers `airconId`, `macAddress`, `apMode`. Use this to bootstrap: it is the only call that works before you know the `airconId`. |
| `getAirconStat` | `airconId` | Returns the current state blob |
| `setAirconStat` | `airconId`, `airconStat` (base64) | Returns the *new* state blob |
| `updateAccountInfo` | `accountId`, `airconId`, `remote` (0/1), `timezone` | Registers your operator id on the device |
| `deleteAccountInfo` | `accountId`, `airconId` | Frees a slot |
| `setNetworkInfo` | `ssid`, `netPass` | Wi-Fi provisioning (AP mode) `[FW]` |
| `updateFirmware` | *(not documented here)* | Firmware updating is the app's job — see §8 for why you do not want to drive this yourself |

Additionally, the MQTT (cloud) side knows `setTopicId`, `putAirconStat` and
`reportAirconStat`. These are **not** reachable locally. `[FW]`

### 2.5 The two hard rules

Both are enforced in firmware and both produce confusing failures if you get
them wrong.

1. **`airconId` is mandatory in the body of every relevant call.** `[FW]`
   `json_command_getAirconStat` and `…putAirconStat` check, in order: `contents`
   is non-NULL → `contents` is a JSON object → `get_string(contents,"airconId")`
   is non-NULL and non-empty. Any failure aborts before anything happens. Newer
   firmware answers `HTTP 400` with `result: 2`. Older firmware happens to
   tolerate its absence, which is why some clients omit it and "work".

2. **`setAirconStat` additionally validates `operatorId`.** `[FW]` It is compared
   against the up-to-four accounts stored in flash (table stride `0x68`) and
   against the literal string `aws`. An unknown `operatorId` aborts the handler.
   So: call `updateAccountInfo` once with your own UUID before you try to
   *write* anything. Reading works without registration.

   The `aws` escape hatch is real: `operatorId: "aws"` was accepted for
   `setAirconStat` on a `WF-RAC-HTTPS 025` device without registering anything
   `[HW]`. Handy for one-off diagnostics — it consumes no account slot — but
   register properly for anything permanent.

   `remoteList` does **not** let you discover the registered ids: a device with
   `numOfAccount: 2` returned `["", "", "", ""]`. `[HW]`

   Four account slots exist. A device that has been paired with several phones
   can be full; `deleteAccountInfo` frees a slot.

### 2.6 Rate limiting

The module is single-threaded about connections and says so: the firmware
contains `429 Too Many Requests`, `Connection rate exceeded`,
`device lock timeout: %d` and `send_message_unlock_semaphore_timer`. `[FW]`

**Practical rule, confirmed in the field:** one connection at a time, at least
**1 second** between requests, per device. `[HW]` Violating it produces dropouts
that look like a flaky network.

### 2.7 Response fields (`getAirconStat` / `setAirconStat`)

In the order the firmware emits them: `[FW]`

| Field | Meaning |
| --- | --- |
| `airconId` | MAC-derived device id |
| `airconStat` | **base64 state blob — the actual payload, see §3** |
| `logStat` | bit 2 of an internal flag byte |
| `updatedBy` | `local` if the last change came from the local API, otherwise the account id |
| `expires` | account expiry |
| `autoHeating` | frost-protection flag (see §6.3) |
| `firmType` | hard-coded in the image, e.g. `WCBN4612L` |
| `timezone` | |
| `highTemp`, `lowTemp` | `0xFF` is emitted as JSON `null`; in practice constant `"AB"`/`"66"` (see §6.3) |
| `wireless.firmVer` | `%03d`-formatted counter, e.g. `025` |
| `mcu.firmVer` | bridge MCU version, e.g. `200` |
| `remoteList` | registered accounts |
| `numOfAccount` | |

`updatedBy` does **not** distinguish IR remote from Wi-Fi changes — it is
constant `local` in all recordings. `[HW]`

### 2.8 The complete field inventory

The `WCBN4612L` module image contains exactly these JSON key strings and no
others `[FW]`. **This is closed for that branch only.** The `WF-RAC` and
`WF-RAC-HTTPS` images are encrypted and could not be inventoried — and a live
`WF-RAC-HTTPS 025` device returns two fields that are *not* in this list,
`ledStat` (integer) and `mcu` (object, `{"firmVer": "200"}`) `[HW]`. Expect
per-branch additions:

`command`, `apiVer`, `operatorId`, `deviceId`, `timestamp`, `contents`,
`result`, `airconId`, `airconStat`, `accountId`, `macAddress`, `apMode`,
`logStat`, `updatedBy`, `expires`, `autoHeating`, `firmType`, `highTemp`,
`lowTemp`, `wireless`, `firmVer`, `remoteList`, `numOfAccount`, `timezone`,
`remote`, `firmUrl`, `cksum`, `ssid`, `netPass`, `topicId`, `aggTm`.

Four of them have a **known transport but an unverified effect** — the module
demonstrably reads and forwards them, but nobody has confirmed that writing them
changes anything on the AC. Treat them as unknown, not as features:

| Field | What is certain | What is not |
| --- | --- | --- |
| `logStat` (request) | read on `setAirconStat`, value forced below 2, stored in an internal flag bit, echoed in the response `[FW]` | what the bit switches on |
| `autoHeating`, `highTemp`, `lowTemp` (request) | placed into the UART frame header to the bridge MCU, at header bytes `[3]` bit 0, `[4]`, `[5]` `[FW]` | whether the AC acts on them. Responses are constant `0` / `"AB"` / `"66"` on every device measured, and the official app never writes them locally (§6.3) |
| `deviceId` (value) | mandatory, logged, handed to the command handler `[FW]` | whether any handler evaluates it, or whether it only has to be present |
| `timezone` | stored per account, echoed back `[FW]` | what the module uses it for — `expires` formatting and `aggTm` are the obvious candidates, neither traced |

---

## 3. Layer 2 — the `airconStat` blob

Base64. Decoded, it is **two independent blocks back to back**, each with its
own CRC16: `[APP]` `[FW]`

```
┌─────────────── COMMAND block ──────────────┬─────────────── RECEIVE block ──────────────┐
│ 18 B state │ 1 B segCount │ segCount×4 B │ 2 B CRC │ 18 B state │ 1 B segCount │ … │ 2 B CRC │
└────────────────────────────────────────────┴────────────────────────────────────────────┘
```

- **COMMAND block** = what you want the AC to do (and, in a device response, an
  echo of the last command it accepted). This is the block the bridge MCU
  translates into MISO frames.
- **RECEIVE block** = what the AC currently *is*. This is the block you parse.

### 3.1 Locating the RECEIVE block

The COMMAND block is variable-length, so:

```python
seg_count      = data[18]                 # segment count of the COMMAND block
receive_start  = seg_count * 4 + 21       # 18 state + 1 count + n*4 + 2 CRC
state          = data[receive_start : receive_start + 18]
rx_seg_count   = data[receive_start + 18]
segments       = data[receive_start + 19 : receive_start + 19 + rx_seg_count*4]
```

### 3.2 CRC16

CRC16-CCITT, appended **little-endian** to each block: `[APP]`

| Parameter | Value |
| --- | --- |
| Polynomial | `0x1021` |
| Init | `0xFFFF` |
| Reflect in / out | no / no |
| Final XOR | none |
| Covers | the block's own bytes only (state + count + segments) |

The bridge MCU verifies the COMMAND block's CRC before doing anything with it.
`[FW]`

### 3.3 The "nothing to send" sentinel

When it has no variable data to transmit, the official app appends
`[0x01, 0xFF, 0xFF, 0xFF, 0xFF]` — segment count `1`, then one segment whose
code byte is `0xFF`. `[APP]` The bridge treats code `0xFF` as "no request" and
skips the whole request path. `[FW]` Use this as your default trailer.

---

## 4. Layer 3 — the 18-byte state block **is** the CNS/SPI frame

This is the key insight for anyone coming from the ESP32 side.

The bridge MCU copies the state block straight out of the MOSI frame: `[FW]`

```
state[0..9]   = MOSI raw byte  1..10
state[10..17] = MOSI raw byte 20..27
```

Since the MHI frame is `SB0 SB1 SB2 DB0 DB1 …`, that gives:

### 4.1 Byte map

| `state[i]` | SPI | Contents | Confidence |
| --- | --- | --- | --- |
| `0` | `SB1` | `0x80 \| ModelNr` — the signature byte doubles as a model id, read it as `state[0] & 0x7F` | `[FW]` `[HW]` |
| `1` | `SB2` | constant `0x04`, no known use | `[FW]` |
| `2` | `DB0` | power / mode / up-down swing | `[HW]` |
| `3` | `DB1` | fan speed / vane position | `[HW]` |
| `4` | `DB2` | setpoint | `[HW]` |
| `5` | `DB3` | room temperature in use, `(raw−61)/4` °C | `[EXT]` `[HW]` |
| `6` | `DB4` | error code | `[HW]` |
| `7` | `DB5` | **undocumented.** Bit `0x10` active, but constant across every state measured so far | `[HW]` |
| `8` | `DB6` | operation-data type/echo; bit `0x08` = "cool/hot judge" off | `[HW]` |
| `9` | `DB7` | bit `0x02` = **compressor running** (see §4.6) | `[HW]` |
| `10` | raw 20 | `bit0` occupancy/`Vacant`, `bit2` self-clean reset | `[HW]` |
| `11` | raw 21 | left-right vane position (low 5 bits) | `[HW]` |
| `12` | raw 22 | 3D-auto/`Entrust`, LR-vane enable, self-clean flags | `[HW]` |
| `13`–`14` | raw 23–24 | unknown | — |
| `15` | raw 25 | `bit0` self-clean operation (see §6.4) | `[HW]` |
| `16`–`17` | raw 26–27 | unknown | — |

`state[10..17]` live **beyond** the classic 20-byte frame. WF-RAC-capable units
send extended frames; this is where left/right vanes and 3D-auto are carried.
MHI-AC-Ctrl reaches part of the same area as its `DB16`/`DB17`. `[EXT]`

### 4.2 Decoding the state block (read path)

```python
Operation      = (state[2] & 0x03) == 1              # power on
OperationMode  = {0x08:'auto', 0x10:'cool', 0x0C:'dry', 0x04:'fan', 0x00:'heat'}[state[2] & 0x3C]
SwingUD_on     = (state[2] & 0xC0) == 0x40
AirFlow        = {0x07:'auto', 0x00:1, 0x01:2, 0x02:3, 0x06:4}[state[3] & 0x0F]
VaneUD         = (state[3] & 0xF0) >> 4              # 0..3 -> position 1..4, ignored if swinging
PresetTemp     = state[4] / 2.0                      # °C
IndoorTemp_raw = state[5]                            # see §4.5 — but prefer the pushed segment
ErrorCode      = state[6]                            # see §4.4
CoolHotJudge   = (state[8] & 0x08) == 0
Vacant         = (state[10] & 0x01) != 0
VaneLR         = (state[11] & 0x1F) + 1              # ignored if (state[12] & 0x03) == 1
Entrust3D      = (state[12] & 0x0C) == 0x04
```

Mode bit layout is `DB0[4:2]` and the fan bits are `DB1[1:0]`, exactly as in the
SPI documentation `[EXT]`; the tables above are the WF-RAC-observed values `[HW]`.

### 4.3 Building the COMMAND block (write path) — and the set-bit trap

The COMMAND block is copied byte-for-byte into the MISO frame with the same
offset: `command[i] → MISO raw byte i+1`, so `command[2] → DB0`, and so on. `[FW]`

On the MHI bus, **writing a field means setting its accompanying set-bit**. A
value alone is ignored. Those set-bits are:

| Field | Value bits | Set-bit | In `command[]` |
| --- | --- | --- | --- |
| Power | `DB0[0]` | `DB0[1]` | `command[2] \|= 0x02` (+`0x01` for on) |
| Mode | `DB0[4:2]` | `DB0[5]` | `command[2] \|= 0x20` |
| Vane up/down swing | `DB0[6]` | `DB0[7]` | `command[2] \|= 0x80` |
| Vane up/down position | `DB1[5:4]` | `DB1[7]` | `command[3] \|= 0x80` |
| Fan | `DB1[1:0]` | `DB1[3]` | `command[3] \|= 0x08` |
| Setpoint | `DB2[6:0]` | `DB2[7]` | `command[4] = temp*2 \| 0x80` |
| Room temperature | `DB3[7:0]` | *(none)* | `command[5]`, `0xFF` = leave internal sensor |

This is why the WF-RAC command tables look "off by a constant" compared with the
receive tables — e.g. mode `auto` is `0x20` when writing and `0x08` when reading.
The extra bits *are* the set-bits.

A complete, safe command block: start from
`[0,0,0,0,0,0xFF,0,0,0,0,0,0,0,0,0,0,0,0]`, set only the fields you want to
change, append the sentinel trailer of §3.3, CRC it, then append a RECEIVE block
built the same way (the official app sends both; the device ignores the receive
half). `[APP]`

**Never leave `command[5]` at anything other than `0xFF` unless you mean it** —
see §5.5.

### 4.4 Error code (`state[6]`)

```python
code = state[6] & 0x7F
if state[6] & 0x80:  label = f"M{code:02d}"   # indoor/self-diagnosis family
elif code == 0:      label = "00"             # no error
else:                label = f"E{code}"       # E-family
```

Bit 7 selects the family, and the bit-7 test must come **before** the
zero test. `[APP]` Getting this wrong is a well-worn bug: a naive
implementation makes the `E` branch dead code.

### 4.6 `state[9] & 0x02` — compressor demand

Not documented anywhere else. It carries the distinction between *powered on*
and *actually calling for the compressor* — the AC reports `hvac_action:
cooling` either way, and this bit costs nothing to read, since it rides along
in every ordinary status poll with no operation-data request needed.

Correlated against operation-data code `0x11` (compressor frequency) across a
multi-day recorder history of two indoor units on one outdoor unit `[HW]`:

| Situation | `state[9] & 0x02` | Code `0x11` |
| --- | --- | --- |
| Unit calling for compressor | 1 | > 0 Hz |
| Unit satisfied, sibling unit also idle | 0 | 0.0 Hz |
| Unit satisfied, **sibling still calling** | 0 | > 0 Hz |

Both directions are covered. Where no sibling was demanding, the bit and the
frequency moved together within a single poll: five clean 1 → 0 transitions,
each followed by the frequency reading 0.0 Hz two seconds later, and the
matching 0 → 1 transitions with the frequency picking up in the same poll.

**The apparent counterexamples are the important part.** In four further cases
the bit dropped to 0 while the frequency stayed at 20–35 Hz. Every one of them
lines up with the *other* indoor unit still demanding at that moment. That is
the relationship worth remembering:

- `state[9] & 0x02` is **per indoor unit** — does this unit want the compressor.
- Code `0x11` is **outdoor-unit-level** (§5.4) — is the shared compressor
  turning, for whoever asked.

On a single-split system the two are interchangeable. On a multi-split they are
not, and the bit is the one that answers "is this room being served".

`state[7] & 0x10` stayed set in every sample, at 0 Hz and at full load alike,
so whatever it means, it is not compressor state.

### 4.5 Temperatures — prefer the segments over `state[5]`

`state[5]` is `DB3`, the *room temperature the controller is currently working
with*, in the SPI encoding `T = (raw − 61) / 4`, 0.25 °C steps `[EXT]`. It is
populated on a live device — verified against a capture where `state[5] = 0x9A`
⇒ 23.25 °C while the indoor-temperature segment read 23.5 °C `[HW]`. Usable as a
coarse fallback, and useful as a plausibility check.

**The `0xFF` convention runs the other way.** `0xFF` means "no external value" in
the *write* direction (`command[5]`, §5.6); in the read direction the AC reports
the temperature it is actually using.

The temperature you actually want arrives as **pushed variable segments** (§5.2)
with much better resolution: 256-entry thermistor lookup tables, 0.1 °C steps,
−30…52 °C indoor and −50…43 °C outdoor. These tables are non-linear and are not
derivable from a formula — copy them (the ones in this project live in
`repo/custom_components/mitsubishi_wf_rac/wfrac/utils.py`). `[APP]`

---

## 5. Layer 4 — variable segments: the operation-data channel

This is the part the official app barely uses, and the most valuable part of the
interface.

### 5.1 Segment format

Every variable segment is **4 bytes**, and it is literally `DB9…DB12` of an SPI
frame: `[FW]`

```
[ Code, OP1, OP2, OP3 ]  ==  [ DB9, DB10, DB11, DB12 ]
```

| `OP1` | Meaning |
| --- | --- |
| `0x10` (16) | status report — this segment carries a value |
| `0xFF` (255) | **request** — "tell me this" |
| `0x00` (0) | **command** — "set this" |

`OP2`/`OP3` are the value, or the sub-selector, depending on the code.

> **Safety note.** `OP1 = 0` writes into the AC main board through a path nobody
> outside MHI has mapped. If you are exploring, send `0xFF` only.

### 5.2 What arrives unsolicited

The bridge keeps a cache of exactly **three** slots, hard-coded in ROM `[FW]`:

| Code | `OP1` | Meaning | Decoding |
| --- | --- | --- | --- |
| `0x80` | `0x10` | outdoor temperature | `outdoorTempTable[OP2]`, 0.1 °C |
| `0x80` | `0x20` | indoor temperature | `indoorTempTable[OP2]`, 0.1 °C |
| `0x94` | `0x10` | energy meter | `(OP3<<8 \| OP2) × 0.25` kWh |

These appear in the RECEIVE block's trailer on ordinary polls, without being
asked for. `[HW]`

The energy counter is **per run, not lifetime**: it counts up in 0.25 kWh steps
while the indoor unit is running, holds its last value while the unit is off,
and is cleared to 0 at the next power-on — the value read right after an
off→on transition is 0. `[HW]` (Six resets across two units, each one within a
minute of an off→on transition, including one initiated from the unit itself
rather than over the network; no reset ever occurred without a power-on, and
ordinary `setAirconStat` commands do not clear it.) Treat it as a counter that
may restart at any time: accumulate its upward steps yourself if you want a
lifetime figure, and never read a low value as a fault.

On a multi-split the counter is **per indoor unit**, not a shared outdoor-unit
total, so the values of two units may be summed. `[HW]` (Verified on a
two-unit multi-split: one unit cleared its counter at its own power-on while
the sibling's kept running unchanged; the two accumulated at different rates
over the same interval; and one kept counting while the other was off for
seven hours. Do not conclude the opposite from a single side-by-side run —
two units cooling in parallel accumulate at nearly the same rate and look
like one shared meter.)

### 5.3 Requesting anything else — the generic path

The bridge copies the COMMAND block's variable segments **verbatim and
unfiltered** into a request queue, emits one per MISO frame with the
operation-data type bit set in `DB6`, matches incoming MOSI answers by code
byte, and appends the matches to the RECEIVE block's trailer. `[FW]`

There is no whitelist on this path. The only special case is the *matching*
step: for code `248` the sub-code in `OP2` is compared as well, because that
code alone would not identify which of six answers came back.

**Recipe** — verified end to end on real hardware `[HW]`:

```
1. build a COMMAND block for the *current* state (change nothing)
2. trailer = [1] + [code, 0xFF, 0xFF, 0xFF]        # one request
3. CRC, base64, POST setAirconStat
4. the answer is ALREADY in that POST's own response:
   receive trailer -> [code, sel, value, value2]
```

**Read the answer out of the `setAirconStat` response.** The round trip over the
CNS bus finishes inside the HTTP request, so the reply block you get back
already carries the extra segment. It is *transient*: the bridge clears its
response cache after handing it over, and a `getAirconStat` ~2.5 s later shows
the usual three segments and nothing else. `[HW]` If any other client polls the
device in that window, it — not you — gets the answer.

Two things that do **not** work as one might expect `[HW]`:

- **The command-block echo does not reflect your trailer.** It stays at the
  `0xFF` sentinel, so it cannot be used as an "was my request accepted"
  channel. The answer itself is the only confirmation.
- **You cannot choose the unit.** The bridge fixes the `DB6` selector, and the
  reply tells you which sensor you got via its own `sel` byte (`0x10` vs
  `0x20`, the same convention as air temperature in §5.2). Codes that need a
  selector the bridge cannot send answer with all-`0xFF` (see `0x1E`/`0x1F`).

**Limits.** The bridge does not clamp the segment count and copies `count × 4`
bytes into a queue with room for 22 entries. `[FW]` Keep the count small (1–3);
a large count is a buffer overrun on a device you cannot debug.

**Timing.** SPI frames run at roughly 20/s `[EXT]`, one request per frame, so a
handful of requests resolve in well under a second. Anything slower than that is
your own poll interval, not the device.

### 5.4 Operation-data codes

The bridge firmware contains per-code plausibility rules for these codes `[FW]`.
**All fifteen were requested over the WF-RAC HTTP path and all fifteen
answered** (SRK35ZS-WF, `WF-RAC-HTTPS 025/200`) `[HW]`. Measured twice:
once with the compressor idle, once under load (setpoint forced 6 K below room
temperature). Where a value moved sensibly between the two, that is noted —
those readings are consistent with the MHI-AC-Ctrl formulas `[EXT]`, but two
operating points are not a calibration, so the formulas stay `[INF]`.

| Code | Name | Formula | Measured (compressor idle) |
| --- | --- | --- | --- |
| `0x11` | Compressor frequency [Hz] | `(sel − 0x10) × 25.6 + 0.1 × OP2` | idle `00` ⇒ 0.0 Hz · load `e6` ⇒ **23.0 Hz** |
| `0x90` | Current [A] | `OP2 × 14 / 51` | idle `00` ⇒ 0.0 A · load `04` ⇒ **1.10 A** (≈250 W) |
| `0xB1` | TDSH [°C] | `OP2 / 2` | `00` in both — never moved |
| `0x85` | Discharge pipe TD [°C] | `OP2 / 2 + 32` | idle `14` ⇒ 42 °C · load `17` ⇒ **43.5 °C** |
| `0x13` | Outdoor EEV [pulses] | `OP2` | idle `c8` ⇒ 200 · load `5f`/`66` ⇒ **95 / 102** (valve closes under load) |
| `0x82` | THO-R1 | outdoor heat exchanger, **no known conversion** | idle `3f` ⇒ 63 · load `49` ⇒ 73; tracks the outdoor unit, but not on the coil scale below |
| `0x81` | THI-R1 [°C] | `outdoorTempList[2 × OP2]` | `20 5a ff` ⇒ raw 90, **`sel` = `0x20`** |
| `0x87` | THI-R3 [°C] | `outdoorTempList[2 × OP2]` | `10 5a ff` ⇒ raw 90 |
| `0x1E` | Total run hours [h] | `OP2 × 100` | `ff ff ff` ⇒ **no value** |
| `0x1F` | Fan speed | `OP2` | `ff ff ff` ⇒ **no value** |
| `0x0D` | *unknown* | — | `00 ff ff`, `sel` = `0x00` |
| `0x21` | *unknown* | — | `10 ff ff` ⇒ no value |
| `0x32` | *unknown* | — | `2e 04 ff`, unusual `sel` |
| `0x34` | *unknown* | — | `11 01 04`, **uses `OP3`** — multi-byte value |
| `0x35` | *unknown* | — | `10 00 ff` |
| `0x80` | Air temperature | see §5.2 | pushed unsolicited |
| `0x94` | Energy [kWh] | see §5.2 | pushed unsolicited |

Notes on the shape of the answers `[HW]`:

- The second byte is a **selector**, not a fixed status marker. `0x10` and
  `0x20` follow the same indoor/outdoor convention as air temperature; `0x0D`
  and `0x32` answer with values outside that pattern and are not understood.
- `0x1E` and `0x1F` answer with all-`0xFF`. Both are codes MHI-AC-Ctrl requests
  *twice*, once per unit, using the `DB6` selector the bridge does not expose —
  the likeliest reading is "unit not selectable, so no value". Untested.
- `0x34` is the only code that used `OP3`, so at least one value here is wider
  than one byte.
- On a multi-split installation, `0x11`/`0x90`/`0x85` (compressor frequency,
  current, discharge temperature) read identically from every indoor unit
  sharing the outdoor unit — they are outdoor-unit-level values, not
  per-indoor. `0x13` (EEV) differs per indoor unit, since each has its own
  expansion valve. Confirmed live: one indoor unit cooling, the other idle,
  identical frequency/current/temperature on both, EEV 57 vs. 0. `[HW]`
- `0x13`'s full-open pulse count is unknown. MHI-AC-Ctrl reads the same
  quantity as 16 bits (`DB12<<8 | DB11`, its `OU-EEV1`), while this bridge's
  `OP2` is a single byte — likely a narrowed/derived value, not a raw pass
  of the same counter. No reference project (MHI-AC-Ctrl or its ESPHome
  port) converts it to a percentage either; both publish raw pulses. `[EXT]`
  Mapping the byte linearly onto 0–100 % is fine as a *relative* reading —
  idle against load, or one indoor unit against another — but do not present
  it as a calibrated valve opening.
- `0x13` reads 0 on an indoor unit whose compressor is not running, and a
  normal value on an active one at the same moment. `[HW]`
- **The two indoor coil sensors are per indoor unit; `0x82` is shared.** Over a
  night with one indoor unit cycling and the other cooling continuously, both
  units' `0x81` collapsed when their *own* compressor demand was on, while
  `0x82` read the same on both (paired correlation 0.89). `0x87` follows `0x81`
  per unit: identical while the compressor is off, higher while it runs — the
  difference is evaporator superheat. `[HW]`
- **Conversion for `0x81`/`0x87`:** `outdoorTempList[2 × OP2]` — the same
  thermistor table §5.2 uses for outdoor air, indexed at half resolution.
  Anchored on two independent fixed points ~23 K apart, both matching within
  ~1 K `[HW]`: the last reading before every compressor cut-off in cooling
  lands on the service manual's 1.0 °C frost-protection threshold, and after a
  long standstill both indoor coils settle on the separately measured room
  temperature. **Only valid in cooling, and the heating end is now known to be
  wrong.** In a heating run `OP2` climbs to at least 252, far past the table's
  last index, and neither candidate survives: extended linearly the byte would
  mean 103 °C, while the discharge pipe read 57 °C at the same moment — a
  condenser cannot be hotter than the gas feeding it. MHI-AC-Ctrl's
  `value × 0.327 − 11.4` for THI-R1/THI-R3 (its own comment calls it "only a
  rough approximation") fits the top plausibly but misses the bottom by 5 K.
  Taken together the sensor is a real thermistor curve, roughly 0.5 K per count
  low and 0.33 K per count high, and the table is a local approximation of its
  lower half. `[HW]` `[INF]`
- **Because of that, both bytes are published raw as well.** Converting only
  inside the anchored band and reporting nothing above it keeps the reading
  honest, and the raw byte is what the rest of the curve has to be calibrated
  against — a thermometer on the coil during a heating run. Anyone decoding
  this should work from the byte, not from the converted value. `[INF]`
- `0x82` is *not* on that scale — a resting outdoor coil reads far lower than a
  resting indoor coil at the same temperature. MHI-AC-Ctrl documents no formula
  for THO-R1 either. `[EXT]` Treat it as a raw byte.
- **Not every firmware branch answers usefully.** On `mcu131`/`wireless010`,
  `0x11` and `0x90` (compressor frequency, operating current) have been
  reported to return a constant 0 even with the compressor confirmed running,
  while `0x85` and `0x13` return real, unit-specific values on those same
  units. `[EXT]` Both formulas produce exactly `0.0` for `OP1 = 0x10,
  OP2 = 0`, so a decoded zero cannot be told apart from a genuine one — keep
  the raw bytes if you need to distinguish "no data" from "not running".

Codes that MHI-AC-Ctrl uses but that are **absent** from the bridge's list, and
therefore doubtful over this path: `0x7C` (protection number), `0x0C` (defrost).
`[FW]` `0x7C` is now requested alongside the rest — it sits in the same
operation-data address space, and a code the module does not serve simply
leaves its value empty, which costs nothing. Whether any unit answers it is
still open. `0x0C` is not requested.

### 5.5 Code `248` — the one the app does use

The official app's only use of the request channel is the "auto-heating /
away-mode" settings screen. `[APP]`

| Sub-code (`OP2`) | Meaning |
| --- | --- |
| 27 / 28 | outdoor-temperature rule, cooling / heating |
| 29 / 30 | target room temperature, cooling / heating |
| 31 / 32 | airflow, cooling / heating |

- read: send `[248, 0xFF, sub, 0x00]` — six segments, one per sub-code
- write: send `[248, 0x00, sub, value]`
- answers come back as `[248, 0x10, sub, value]`
- temperatures are `value / 2` °C, exactly like the setpoint
- airflow raw bytes map `(0, 3, 5, 7, 14) → auto, 1, 2, 3, 4`

Verified end to end against real hardware, including a write round-trip. `[HW]`
That makes it the working reference implementation for §5.3: same mechanism,
different code byte.

### 5.6 Injecting an external room temperature

`command[5]` reaches the bus as `DB3`. Writing a value below `0xFF` makes the AC
use it **instead of** its built-in sensor `[EXT]`; the encoding is
`raw = round(T × 4) + 61`.

The official app never writes this byte — it is fixed at `0xFF`. `[APP]` The
path is open on the WF-RAC interface `[FW]`, but **it is untested `[INF]`**, and
it is a real control command, not a query. This is the single most requested
reason for replacing the module with an ESP32, and it may not require replacing
anything.

---

## 6. What you cannot get, and why

### 6.1 Set-bits are filtered out

Before handing the state block to the network side, the bridge masks it with an
18-byte ROM table. `[FW]` The mask is

```
00 00 A2 88 80 00 00 00 00 00 00 10 0A 00 00 00 00 00
```

and the bits it clears are precisely the MISO set-bits of §4.3 —
`DB0` bits 7/5/1, `DB1` bits 7/3, `DB2` bit 7 (plus two bits in `state[11]` and
`state[12]`). You cannot see which field was last written, over any API.

*(This mask is also the independent proof of the whole §4 mapping: an 18-byte
table whose set bits land exactly on the documented SPI set-bits cannot be a
coincidence.)*

### 6.2 `DB8`–`DB14` never leave the module

The state block takes MOSI bytes 1–10 and 20–27; bytes 11–19 are dropped. `[FW]`
That includes `DB13`, the byte whose bits are believed to carry outdoor-unit
status including compressor running/idle `[EXT]`.

That is a real limit, but it does **not** mean compressor state is unreachable:
it also rides in `DB7` bit 1, which *is* forwarded — see §4.6. Do not
generalise "`DB13` is dropped" into "outdoor status is unavailable"; check the
forwarded bytes first.

### 6.3 `autoHeating` / `highTemp` / `lowTemp` are not what they look like

These three are carried in the **UART frame header** between the Realtek and the
RL78, not in the `airconStat` blob, and surface as top-level JSON fields. `[FW]`

Measured across four devices, three accounts and two firmware branches:
`highTemp` and `lowTemp` come back as the constants `"AB"` and `"66"`, and
`autoHeating` is always `0`. `[HW]` The official app never writes the local
request fields either (they stay `null`); the only code that sets a
`highTemp`/`lowTemp` pair does so on a **cloud** endpoint. `[APP]`

Treat them as inert unless you can prove otherwise on your unit.

### 6.4 Self-clean cannot be started remotely

No code path in the app sends "start self-clean"; only a reset/cancel bit exists
(`command[10]` bit `0x04`). `[APP]` The status bit `state[15] & 0x01` does **not**
track the real cycle: driven from the IR remote, it stayed `0` through a
complete self-clean run while the module was polled throughout. `[HW]` It echoes
the last value written over Wi-Fi, nothing more.

### 6.5 The cloud adds no data

The MQTT reporter and the local `getAirconStat` handler call the *same* builder
for the `contents` object; the only difference is that the MQTT report appends
`aggTm`. `[FW]` The cloud's hourly/daily/monthly "operation data" is a
server-side aggregation of exactly what you can read locally. `[APP]`

### 6.6 The hourly reset is not the module's idea

The module resets its CPU from exactly two places: a one-shot 10 s timer used
after provisioning, and a `module_reset()` whose only caller is the branch that
handles "cpu reset received" — a bit in an incoming frame from the bridge.
Every timer in the module firmware is in the seconds range; no hour-scale
constant exists in the application area. `[FW]` So the periodic reset seen in
the field is commanded from below, not by the Wi-Fi stack. Blocking the module's
WAN access does not stop it. `[HW]`

---

## 7. Worked flows

### 7.1 Read state

```
POST /beaver/command/getDeviceInfo          -> airconId
POST /beaver/command/getAirconStat {airconId}
  -> contents.airconStat (base64)
     decode, skip COMMAND block (§3.1), parse 18 state bytes (§4.2)
     parse trailer segments (§5.2) for temperatures and kWh
```

### 7.2 Change the setpoint to 22.0 °C

```
1. read current state (7.1)                     # you must send a full state
2. command = current 18 bytes re-encoded
3. command[4] = int(22.0 * 2) | 0x80            # value + set-bit
4. trailer  = [0x01, 0xFF,0xFF,0xFF,0xFF]       # sentinel
5. blob = crc16(command + trailer) + crc16(receive + trailer)
6. POST setAirconStat {airconId, airconStat: base64(blob)}
```

Registering your `operatorId` via `updateAccountInfo` is a prerequisite (§2.5).

### 7.3 Ask for compressor frequency

```
1. build a no-op command block (current state, no set-bits)
2. trailer = [0x01, 0x11, 0xFF, 0xFF, 0xFF]
3. POST setAirconStat
4. sleep 1 s
5. POST getAirconStat, scan the RECEIVE trailer for a segment whose code is 0x11
   -> Hz = (OP1 - 0x10) * 25.6 + 0.1 * OP2
```

Untested `[INF]`. If it works, the same shape works for every code in §5.4.

---

## 8. Firmware versions

Three parallel version lines exist. **Version numbers are only comparable within
one `firmType`** — a higher number in another branch means nothing. This trips
people up constantly when comparing notes across units.

| `firmType` | Wireless (`M`) | MCU (`C`) |
| --- | --- | --- |
| `WF-RAC` | 010 | 131 |
| `WF-RAC-HTTPS` | 025 | 200 |
| `WCBN4612L` | 029 | 999 |

Both numbers are reported by `getDeviceInfo` as `wireless`/`mcu`, alongside
`firmType`. Quote all three when reporting a problem — the pair alone is
ambiguous.

Updating is the app's job and is deliberately **not** documented here. Two
things are worth knowing before anyone goes looking:

- The bridge MCU update path is **fire and forget** — no progress reporting, no
  rollback, no second bank. A failed write does not leave you with a device to
  retry on.
- The device rejects downgrades: if the requested version is not newer than the
  running one, the handler returns `200 OK` and does nothing. `[FW]`

---

## 9. Quick answers to the usual questions

| Question | Answer |
| --- | --- |
| Can I run an ESP32 on CNS *and* keep the WF-RAC module? | No. Two active slaves on the bus conflict. A purely passive sniffer that never drives MISO is fine. `[EXT]` |
| Is there a local MQTT broker? | No. MQTT is cloud-only, endpoint fetched from `iot.smartmair.com`, mutual TLS with a device certificate in flash. `[FW]` |
| Can I get compressor/current/hours without opening the unit? | Yes, via §5.3 — verified end to end, all fifteen codes in §5.4 answer. Watch the firmware caveat there. |
| Can I feed an external room temperature without opening the unit? | Probably yes, via §5.6. Untested. |
| Does the module do anything smart with the state bytes? | No. It is a base64 pipe. All semantics live in the RL78 and the AC. `[FW]` |
| Why does the energy counter keep dropping to zero? | It is a per-run counter, not a lifetime one (§5.2). Nothing is broken. |
| How do I tell "unit is on" from "compressor is actually running"? | `state[9] & 0x02` (§4.6), free in every poll. On a multi-split it answers per indoor unit, unlike compressor frequency. |
| Why does my client work on one unit and fail on another? | Almost always `airconId` missing (§2.5), TLS too modern (§2.2), or the four account slots being full. |

---

## Appendix A — bridge MCU internals (provenance)

Only needed if you want to re-derive or extend the above. Architecture: Renesas
RL78, 64 KiB image, unencrypted, ~36 KiB occupied. Confirmed with a real
`rl78-elf-objdump` built from GNU binutils. Addresses are RAM in the `0xF____`
segment.

| Address | Contents |
| --- | --- |
| `0xE236` | 18-byte COMMAND state, as received from the Wi-Fi side |
| `0xE248` | COMMAND segment count; segments from `0xE249` |
| `0xE1DE` | request queue, count in `0xFE3B6`, room for 22 segments |
| `0xE186` | response cache, count in `0xFE3B9`, 22 segments |
| `0xE17A` | unsolicited-push cache, 3 slots; ROM table at `0x3025` = `80 01`, `80 00`, `94 01` |
| `0xE2F5` | 18-byte RECEIVE state (what §4 calls `state[0..17]`) |
| `0xE307` | RECEIVE segment count; segments from `0xE308` |
| `0xE3BA` | raw 18 bytes lifted out of the MOSI stream, pre-mask |
| `0xE3CF` | MOSI frame (`SB0` at +0, `DB0` at +3, `DB9..DB12` = `0xE3DB..0xE3DE`) |
| `0xE411` | MISO frame (`DB6` = `0xE41A`, `DB9..DB12` = `0xE41D..0xE420`) |
| `0xE3B7` | request state machine, 0…5 |
| `0x3034` | 18-byte ROM mask (§6.1) |

| Function | Role |
| --- | --- |
| `0x9B28` | unpack COMMAND block from the Wi-Fi buffer (`0xDE85`), verify CRC16 |
| `0x6AB7` | fill the request queue from `0xE248`/`0xE249`; state 1→2 |
| `0x5679` | emit one queued segment per MISO frame into `DB9..DB12`, set the opdata bit in `DB6`; when drained, state→3 |
| `0x53BB` | copy MOSI frame → raw state `0xE3BA`, then call the two below |
| `0x58B1` | handle one received segment: push-cache check, then match against the request queue; all matched ⇒ state 4 |
| `0x5B27` | build the RECEIVE trailer from push cache + response cache |
| `0x680F` | build the RECEIVE state: `state[i] = raw[i] & ~mask[i]` |
| `0x9A44` | pack RECEIVE state + trailer + CRC16 into the Wi-Fi buffer |
| `0x54B7` | translate the COMMAND state into a MISO frame (`command[i] → MISO byte i+1`) |

Reproduce the image with `mcu-fetch.py`; disassemble with `mcu-disasm.py`
(`vectors`, `d <addr> [n]`, `str`, `occupied`). The module image is handled by
`wcbn-disasm.py` (Capstone, Thumb-2).

**Do not flash the bridge MCU.** The update path has no rollback, no progress
reporting and no second bank, and the official app does not even offer the file.
Static analysis only.

---

## Appendix B — sources

| Source | Used for |
| --- | --- |
| `WCBN4612L_M_029.bin` (unencrypted Realtek image, log macros keep file names, line numbers and function names in clear text) | HTTP handlers, MQTT, reset paths, rate limiting, `airconId`/`operatorId` validation |
| `WF-RAC-HTTPS_C_200.bin` (64 KiB RL78 image) | everything in §4, §5, §6.1, §6.2, Appendix A |
| Smart M-Air Android app 1.4.009 (`AirconStatCoder`, `ModelNoType`, `OptionSettingViewModel`, `WifiInterfaceUseCase`) | blob layout, CRC, code `248`, capabilities, thermistor tables |
| [MHI-AC-Trace `SPI.md`](https://github.com/absalom-muc/MHI-AC-Trace/blob/main/SPI.md), [MHI-AC-Ctrl](https://github.com/absalom-muc/MHI-AC-Ctrl) | SPI frame layout, set-bit semantics, operation-data codes and formulas |
| Live captures against SRK35ZS-WF units on `WF-RAC-HTTPS 025/200` | every `[HW]` claim |

Independent client implementations worth reading: `homebridge-mhi-wfrac`,
`mqtt2mhi-wf-rac`, `ioBroker.woso_mitsu_aircon_rac`, `node-red-aircon-rac-wf`,
and this project's own `custom_components/mitsubishi_wf_rac/wfrac/rac_parser.py`.
