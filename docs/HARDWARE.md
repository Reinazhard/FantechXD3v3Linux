# Helios II XD3v3 hardware contract (corrected)

Re-derived 2026-11-09 from `Config.ini`, the vendor's shipped `设备1.png`,
the 14 LED swatches, the XML catalog, and the full cmd-8 flash sweep
reconstructed from `captures/dpi-read-response.pcapng`.

## 1. Six physical buttons

`Config.ini` has 16 `KeyParamN` entries, but only `KeyParam1..6` have
coordinates on the mouse body (`x < 340`). `KeyParam7..16` sit at `x=384`
(right-hand function-list column) or `y=266` (bottom strip) = the vendor's
software assignment list, **not hardware**. The shipped `设备1.png` (432×356)
shows exactly 6 buttons. My earlier 16-hotspot UI was wrong.

| # | Button | Default KeyFunMap | Flash |
|---|--------|-------------------|-------|
| 1 | Left Click | `01 01 00` (reserved) | `0x60` |
| 2 | Right Click | `01 02 00` | `0x64` |
| 3 | Middle / Scroll | `01 04 00` | `0x68` |
| 4 | Macro 1 · **left** (Back) | `01 08 00` | `0x6C` |
| 5 | Macro 2 · **left** (Forward) | `01 10 00` | `0x70` |
| 6 | DPI Cycle (bottom) | `02 01 00` = DPI Loop | `0x74` |

The two macro buttons are on the **left** side (confirmed by the device owner;
the vendor photo is a left-side view). Button 6 is the bottom DPI-cycle button;
its default binding `type=0x02, param=0x0001` is "DPI Loop" from the XML
catalog, so each press cycles to the next configured stage. Left Click cannot
be rebound (`<ReserveLeftKey>Must keep Left key</ReserveLeftKey>`), now enforced
in `ButtonProfile.set_binding`.

## 2. Four DPI stages, each with a unique LED color

* `Config.ini DPIMaxGrade=4`; the vendor UI uses `customComboBox_DPIGrade`.
* `DPIRange=50,26000,50,...` → slider 50–26000, step 50.
* `DPIColor=255,0,0, 0,0,255, 0,255,0, 255,0,255, ...` → per-stage LED RGB.
* The 14 vendor swatches (`res/5灯光界面/颜色N.png`) sample to
  red, cyan, green, magenta, yellow, orange, blue, pink, sky, purple, white,
  lime, amber, rose — the selectable palette.
* `dpi-0800/1600/3200` captures show **sequential register writes** at `0x0c`
  (`0x08,0x09,…0x1f`) = the user dragging the DPI slider; the managed code has
  `customTrackBar_..._ValueChanged`. So the slider writes on drag; "Save to
  stage" commits the value to a stage slot.
* Validation requires 4 unique colors — each DPI stage has its own LED.

Flash map from the cmd-8 sweep (0x00..0x7f):

```
0x00        report-rate enum            (2-byte value+checksum)
0x02        DPI stage count (4)
0x04        active DPI stage index
0x0a        LOD
0x0c + 4*i  DPI stage register (8 physical slots, 4 user-facing)
0x2c + 4*i  per-stage LED RGB (3 bytes + checksum)
0x60 + 4*i  6 button KeyFunMaps (type,p1,p2,checksum)
```

Reconstructed default flash: stages `1000/1000/1600/3200` DPI with colors
red / blue / green / magenta; buttons `Left/Right/Middle/Back/Forward/DPI-Loop`.

**Known limitation:** the 3200–26000 DPI register encoding is not fully
capture-confirmed. Captures prove `xReg = dpi/50 - 1` up to `0x3f` (3200) and
one extended record `3f 3f 55` for a max write. `protocol.dpi_encoding_confirmed()`
flags this; the UI shows a warning above 3200 DPI instead of claiming the value
was written correctly. No byte is invented.

## 3. Connectivity and battery

`Config.ini` decodes to:

```
VID   = Fantech3554        -> 0x3554
M_PID = FantechF536        -> 0xF536  wired mouse (USB-C)
D_PID = FantechF535,F537   -> 0xF535 / 0xF537  2.4 GHz dongles
Sensor=3395   MM=CX52850P  DM=CX52650N  D2M=CH32V305
```

So the USB config interface exists only for **wired USB-C** and the **2.4 GHz
dongle**. **Bluetooth** uses the host Bluetooth controller and exposes no
vendor USB HID config interface — it is *not detectable* from this process and
is reported honestly rather than guessed. The vendor XML confirms wireless
(`无线连接.png` / `有线连接.png`, `<CantPair>Must be in wireless mode to pair`,
`<OnlyWired>The mouse can only be upgraded in wired mode>`).

Battery: cmd-4 `BatteryLevel` reply
`080400000002 46 00 0f81 00...` → `[6]` = percentage (0x46 = 70%),
`[8:10]` = voltage mV big-endian (0x0F81 = 3969 mV). Verified across all 29
captures (values 50–70%, 3871–3974 mV) and live (15%, 3524 mV).
`Config.ini BatteryParam` is the 3050–4110 mV discharge curve; the UI maps it
to a percentage and colors the pill green/yellow/red.

Battery telemetry requires the **mouse link to be awake** (`cmd-3` reply
byte `[6]=01`). When the mouse sleeps, the dongle drops cmd-4 and the UI says
"mouse asleep? — move to wake" instead of showing a fake number.

## 4. What changed in the app

| Item | Before (wrong) | After |
|---|---|---|
| Buttons | 16 hotspots incl. UI list rows | 6 physical buttons, macros on left, bottom DPI cycle |
| DPI stages | 8 steppers | 4 stage tabs + 50–26000 drag slider + Save to stage |
| LED colors | none | 14-swatch vendor palette, one unique color per stage, written to `0x2c+4*i` |
| DPI cycle | not modeled | button 6 = DPI Loop; stage count/index/color all written |
| Connectivity | generic "connected" | wired / 2.4 GHz dongle / Bluetooth-not-detectable |
| Battery | none | cmd-4 % + mV, discharge curve, asleep detection |

## 5. Verification

* `tests/test_hardware_contract.py` pins all of the above (6 buttons, 4 stages,
  flash addresses, palette, battery decode, PID modes).
* Full flash-image round trip read from the live device; UI Apply writes all
  6 button records, 4 stages, 4 colors, stage count and active index with ACK
  verification.
