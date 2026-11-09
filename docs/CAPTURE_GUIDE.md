# Capture verification and protocol recovery

The 29 captures in `captures/` were supplied by the user (recorded months ago,
previously unverified) and verified on 2026-11-09 with the mouse connected.
They are genuine USBPcap recordings of the vendor software on Windows 10
(`Dumpcap 4.7.2`, `\\.\USBPcap1`).

## Verification performed

| Check | Result |
|---|---|
| File type | pcapng v1.0, USB packets with USBPcap header (encap 152) |
| Device in descriptors | `idVendor=0x3554`, `idProduct=0xf535` — matches live `lsusb` |
| Config protocol | SET_REPORT(Feature) `0x21/0x09`, `wValue=0x0208`, `wIndex=1`, 17 bytes |
| Device replies | interrupt-IN `0x82`, 17 bytes |
| Unique SET_REPORT frames | 243 |
| Outer checksum `0x55 - sum(bytes[0:16])` | 243/243 match |
| Inner checksum `0x55 - sum(values)` | 153/153 WriteFlashData records match |
| Zero-padding after record | 153/153 |
| Encoder byte-for-byte reproduction | 243/243 frames |
| Live status poll on connected mouse | `08 03 ... 41` (online) |
| Live read -> write -> read-back | confirmed (UI Apply -> `probe` read-back; DPI stage persisted) |

Capture parsing used a throwaway pcapng EPB/USBPcap-pseudoheader script during
analysis. The permanent results are `protocol.py` (encoder) and the golden
fixtures in `tests/test_protocol_captures.py`; to re-derive frames, parse the
pcapng Enhanced Packet Blocks as described in `docs/PROTOCOL.md`.

## Derived protocol

See `docs/PROTOCOL.md` for the full layout, checksum, command list and flash
field map. Summary:

* 17-byte feature report; report id 8; 16-bit big-endian flash address at
  bytes `[3..4]`; length at `[5]`; data at `[6..]`; outer checksum at `[16]`.
* `checksum(v) = (0x55 - sum(v)) & 0xFF`.
* `WriteFlashData` (cmd 7) carries a record whose last byte is an inner
  checksum of the preceding value bytes.
* Writes need no AES handshake; the 0x01 challenge is for startup config read.

## Capture inventory by action

| Capture | Confirmed write (addr / value) |
|---|---|
| `poll-125/250/500/1000` | `0x00` rate enum `8/4/2/1` |
| `lod-low/high` | `0x0a` LOD `1/2` |
| `dpi-0400/0800/1600/3200` | `0x0c` DPI register `07/0f/1f/3f` |
| `dpi-32000-max` | `0x0c` extended `3f 3f 55` |
| `dpi-slots` / `dpi-slot-writes` | `0x02/0x04/0x10/0x14/0x18` stage config |
| `button-remap` / `button-remap-all` | `0x64/0x68/0x74/0x0140` KeyFunMap records |
| `click-debounce` | `0xa9` 0..20 ms |
| `sensor-motion-sync` | `0xab` |
| `sensor-angle-snapping` | `0xaf` |
| `sensor-ripple-control` | `0xb1` |
| `sensor-peak-performance` | `0xb5/0xb7` |
| `sensor-mode-lp/hp` | `0xb9` |
| `startup-handshake` | cmd 1 challenge, cmd 3/4/0x12 polls, cmd 8 10-byte sweep |
| `*-read-response` | device replies on ep 0x82 |
| `battery-response` | cmd 4 reply with voltage |

## Remaining gap: cold-device flash reads
## Startup unlock and poll clock (worked out on the live device)

The initial capture-only analysis concluded "writes need no handshake" and
"reads need an AES key". Both were wrong; the user reported that nothing
happened. Live diagnosis found the real requirements:

* **Startup unlock**: `startup-handshake.pcapng` shows two `EncryptionData`
  (cmd 1) frames with fixed 4-byte words `28434869` and `35356966`, each
  followed by `PCDriverStatus(1)` and a cmd-3 poll. These are
  `CS_SetPassward1/2` constants from `HIDUsb.dll`, NOT an AES challenge. The
  device replies with its own words (`78d241cc`, `77d7a1cd`) — constant, not
  random. No `AES.dll` key is needed. `HIDTransport.unlock()` replays them.
* **Poll clock**: the vendor's `CS_UsbServer_Thread` continuously sends cmd-3
  polls. Without an interleaved poll, a cmd-7 write is silently dropped (0/10
  echoes); with it, 10/10. The transport drives `clock()` between commands
  and verifies each write's echo, raising `TransportError` on a missing ACK.

After unlock, cmd-8 reads return a 10-byte sliding flash window; each field is
a 2-byte `(value, checksum)` record. Live-verified end-to-end:

```
probe (before):  report_rate=0x01 lod=0x01 debounce=0x08
UI --real Apply: rate=8 lod=2 debounce=12
probe (after):   report_rate=0x08 lod=0x02 debounce=0x0c
restored:        report_rate=0x01 lod=0x01 debounce=0x08
```

No remaining protocol gap for the implemented features.
