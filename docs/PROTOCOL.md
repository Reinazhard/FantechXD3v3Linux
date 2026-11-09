# XD3v3 HID configuration protocol (capture-confirmed)

Derived from the 29 USBPcap captures in `captures/` (Windows 10, Wireshark
4.7.2, device `3554:f535`). All 243 unique SET_REPORT frames are reproduced
byte-for-byte by `src/helios_editor/protocol.py`; see
`tests/test_protocol_captures.py`.

## USB transport

| | |
|---|---|
| Device | `3554:f535` Compx Fantech Helios II (dongle/receiver PID) |
| Config interface | interface 1, HID class |
| Control request | `bmRequestType=0x21`, `bRequest=0x09` (SET_REPORT) |
| `wValue` | `0x0208` — Report Type=Feature(2), **Report ID=8** |
| `wIndex` | `0x0001` — interface 1 |
| `wLength` | 17 |
| Device reply | interrupt-IN endpoint `0x82`, 17 bytes, same framing |

Live Linux path: `/dev/hidrawN` for interface 1 (from `find_config_hidraw()`),
via `HIDIOCSFEATURE(17)` / `HIDIOCGFEATURE(17)` ioctls (no kernel module, no
root). Verified against the connected mouse.

## 17-byte report layout

```
offset  0      1       2    3      4      5        6 .. 5+len    16
        +------+-------+----+------+------+--------+------------+------+
        | 0x08 | cmd   | 00 | addr |      | length | data bytes | chk  |
        |      |       |    | hi   | lo   |        | (len-1 +  |      |
        |      |       |    |      |      |        |  inner chk)|      |
        +------+-------+----+------+------+--------+------------+------+
```

* `[0]` report id `0x08`
* `[1]` command id (DriverLib.UsbCommandID)
* `[2]` reserved `0x00`
* `[3..4]` flash address, **big-endian 16-bit** (`[3]` high, `[4]` low)
* `[5]` record length (number of bytes at `[6..]` including the inner chk)
* `[6..]` value bytes; for WriteFlashData the last data byte is the inner
  checksum of the preceding value bytes
* `[16]` outer checksum of bytes `[0..15]`
* unused bytes zero-padded

## Checksums

```
checksum(values) = (0x55 - sum(values)) & 0xFF      # "0x55-seeded additive"
```

Equivalently `sum(record) == 0x55 (mod 256)`. Verified **243/243** outer
checksums and **153/153** inner checksums across the capture set. This is the
native `CalculateChecksum` symbol in `HIDUsb.dll`.

Two special frame types carry raw data with **no** inner checksum:
`EncryptionData` (0x01, challenge bytes) and `PCDriverStatus` (0x02, 1 byte).
Their `[5]` length field is the wire length of the raw data.

## Commands observed

| id | name | role |
|----|------|------|
| 0x01 | EncryptionData | startup unlock: two fixed password words |
| 0x02 | PCDriverStatus | sent after a config write |
| 0x03 | DeviceOnLine | poll / keepalive; device replies with status |
| 0x04 | BatteryLevel | poll; device replies with voltage level |
| 0x07 | WriteFlashData | write one flash record |
| 0x08 | ReadFlashData | request a 10-byte flash window |
| 0x0E | ClearSetting | poll |
| 0x12 | ReadVersionID | poll |
| 0x17 | GetLongRangeMode | poll |

## Startup unlock and poll clock (required on real hardware)

The single-action captures were recorded **mid-session**, while the vendor
app's `CS_UsbServer_Thread` was already running. A cold device needs two
things those captures do not show explicitly:

1. **Startup unlock** (`startup-handshake.pcapng`, all `*-read-response`):
   two `EncryptionData` (cmd 1) password words, each followed by
   `PCDriverStatus(1)` and a `DeviceOnLine` poll:
   ```
   080100000008 28434869 ...28   # pass1 -> device replies 78d241cc
   080200000001 01 ...49        # PCDriverStatus=1
   0803...                      # cmd3 poll
   080100000008 35356966 ...0b   # pass2 -> device replies 77d7a1cd
   080200000001 01 ...49
   0803...
   ```
   These are `CS_SetPassward1/2` from `HIDUsb.dll` — opaque constants, NOT an
   AES-computed challenge; no `AES.dll` key is needed for this dongle. Verified
   live: without unlock, cmd-8 reads are ignored. `HIDTransport.unlock()`
   replays them.
2. **A poll clock**: the firmware only echoes/accepts config commands when a
   cmd-3 poll is interleaved. A bare cmd-7 write gets no reply; with a poll
   between commands it is echoed 10/10. `clock()` drives this and every write
   is checked against its echo (no silent false successes).

   **Echo queue:** one poll can produce SEVERAL 17-byte reports (a write echo,
   a battery reply, a poll echo). The transport pumps ALL pending reports into
   a queue and matches each by command+address (`_take`). Reading only one
   report per call drops the rest — this caused a 22-record Apply to abort at
   `0x04` ("DPI doesn't apply") and cmd-4 battery echoes to be lost
   ("battery N/A"). Pinned by `test_pending_queue_handles_multiple_echoes`.

After unlock, `read_flash()` returns a 10-byte sliding window; each field is a
2-byte `(value, checksum)` record. Live round-trip verified: `report_rate
1->8`, `lod 1->2`, `debounce 8->12`, then restored to `1/1/8`.

## Flash field map

| addr | len | field | encoding |
|------|-----|-------|----------|
| 0x00 | 2 | report rate | `REPORT_RATE` enum byte: 125Hz=8, 250=4, 500=2, 1000=1 |
| 0x02 | 2 | DPI stage count | index |
| 0x04 | 2 | active DPI stage | index |
| 0x0A | 2 | LOD | 1=1mm, 2=2mm |
| 0x0C | 4 | current DPI | `(xReg, yReg, DPIex)` + inner chk; `xReg = dpi//50 - 1` |
| 0x10.. | 4 | DPI stages | one 3-byte register record each |
| 0x64 / 0x68 / 0x74 .. | 4 | button `KeyFunMap` | `(type, param1, param2)` + inner chk |
| 0xA9 | 2 | click debounce | 0..20 ms |
| 0xAB | 2 | motion sync | 0/1 |
| 0xAF | 2 | angle snapping | 0/1 |
| 0xB1 | 2 | ripple control | 0/1 |
| 0xB5 | 2 | peak performance | 0/1 |
| 0xB7 | 2 | peak timeout | minutes |
| 0xB9 | 2 | sensor mode | 0=LP, 1=HP |

Addresses above `0x00FF` are used for larger config blocks (e.g. `0x0140`
macro records with len 8) — `[3]` carries the high byte.

## Example: set report rate to 1000 Hz

Captured `poll-1000.pcapng`, frame `08070000000201540000000000000000ef`:

```
08          report id
07          WriteFlashData
00 00       reserved + addr high
00          addr low = 0x00 (report rate)
02          record length = 2
01          value = R_1000 enum
54          inner checksum = (0x55 - 0x01) & 0xFF
00 ... 00   zero padding
ef          outer checksum = (0x55 - sum(first 16)) & 0xFF
```

## Golden fixtures

`tests/test_protocol_captures.py` pins 19 captured frames (poll, DPI, LOD,
button remap, debounce, high-address, read requests, status, challenge) plus
4 device responses. `protocol.build_report()` reproduces all 243 unique
captured SET_REPORT frames.

## Delta writes (no Apply lag)

`protocol.profile_to_records(profile)` is the single source of truth mapping a
profile to `(flash_addr, value_bytes, label)` records — shared by the writer
and the UI preview. `protocol.changed_records(baseline, draft)` returns only
records whose bytes differ.

`HIDTransport.write_profile()` caches the last known device state
(`_device_state`, populated by `read_profile`) and writes ONLY the changed
records. A single DPI edit writes one record; a no-op Apply writes nothing.
Sending all 23 records on every Apply caused extreme mouse lag (each record
costs an ACK round-trip plus a poll). On a real device a valid baseline is
REQUIRED: if the mouse is asleep and `read_profile` fails, Apply fails fast
with "wake the mouse" rather than attempting a full write.

Live verification: two staged changes (HP mode + DPI 1200) -> 2 records in
2.09s; read-back confirmed mode=HP and DPI=1200; no-op Apply 0 records /
0.00s; restored to defaults.

## HP / LP sensor mode

`customComboBox_ModeSelect` XML: `0000`=LP, `0001`=HP (`0010`=Corded, unused).
Captures `sensor-mode-lp.pcapng` / `sensor-mode-hp.pcapng` write flash `0xb9`
with `00` / `01` respectively. Exposed in the UI as a segmented control and
included in `profile_to_records` (label "Sensor mode").
