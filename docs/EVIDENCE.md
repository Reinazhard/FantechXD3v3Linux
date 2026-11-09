# Evidence: how the Helios XD3V3 "mouse editor" works

All findings come from static analysis of the shipped installer
`~/Downloads/Helios_II_XD3V3_Software_V1.0_23121900.setup.exe`
(SHA-256 of extracted `Mouse Drive Beta.exe`: `914d46d8…8044b3`).
No device was attached; no runtime execution of the vendor app was performed.
Conclusions are labeled **[OBSERVED]**, **[INFERRED]**, or **[UNVERIFIED]**.

## 1. Packaging

`innoextract --list` on the installer (Inno Setup 6.1.0, unicode) recovered:

| File | Role |
|---|---|
| `app/Mouse Drive Beta.exe` | main application (5.05 MiB) |
| `app/Config.ini` | device + layout + defaults config |
| `app/Description.xml` | copyright (Fantech, 2023) |
| `app/driver_sensor.h` | sensor DPI register-value tables |
| `app/Language/0-English.xml` | UI labels, function catalog, enums |
| `app/res/2按钮界面/*` | button-editor UI assets |

**[OBSERVED]** `Mouse Drive Beta.exe` is a PE32 **.NET Framework 4.8 mixed-mode
WinForms assembly** (`file` → "Mono/.Net assembly"; REA
`inspect_managed_artifact` → runtime `v4.0.30319`, 214 types, 1979 methods,
210 P/Invoke, 40 managed resources). It embeds native DLLs via
**Costura.Fody** — `ManifestResource` names include `costura32.hidusb32.dll`,
`costura64.hidusb.dll`, `costura64.msvcp140.dll`, `costura64.vcruntime140.dll`.
Carving the embedded PE images confirmed `HIDUsb.pdb` in 32- and 64-bit builds.

## 2. The "mouse editor" is a visual button-remapping editor

**[OBSERVED]** `Config.ini [Device1]` defines 16 `KeyParam` rows, but only
`KeyParam1..6` are **physical buttons** on the mouse body. `7..16` are the
vendor's software function-list rows (see `docs/HARDWARE.md`). The shipped
`设备1.png` shows exactly 6 buttons. The corrected 6-button contract is
implemented in `model.py` and pinned by `tests/test_hardware_contract.py`.

```
KeyNumber=60
KeyParam1=127,160,0x01,0x01,0x00   # Left Click
KeyParam2= 31,154,0x01,0x02,0x00   # Right Click
KeyParam3=100,124,0x01,0x04,0x00   # Middle / Scroll
KeyParam4=...                       # macro (left)
KeyParam5=...                       # macro (left)
KeyParam6=...                       # bottom DPI cycle
KeyParam7..16 = x>=384 or y=266      # UI list rows, NOT hardware
```

Format is `x,y,type,param,reserved`. The vendor mouse diagram assets
`res/2按钮界面/设备1.png` and `设备2.png` are exactly **432×356 px** — the same
coordinate space as the `KeyParam` x∈[0,432], y∈[0,356] values. Hotspots 1–5
sit on the mouse body; 6–16 sit on the right-hand function list.

**[OBSERVED]** The default bindings map one-to-one onto the function catalog in
`Language/0-English.xml`:

| KeyParam | default (type,param) | catalog label |
|---|---|---|
| 1 | `0x01,0x01` | Left Button |
| 2 | `0x01,0x02` | Right Button |
| 3 | `0x01,0x04` | Middle Button |
| 4 | `0x01,0x10` | Forward |
| 5 | `0x01,0x08` | Back |

`[Device1]` also carries `DefaultDPI=2`, `DPIMaxGrade=4`,
`DPIRange=50,26000,50,…`, `DPIGrade=400,800,1600,3200,3200,6400,12000,19000`,
`KeyDebounceTime=8`, `Sensor=3395` (base64 `MzM5NQ==`), `BatteryParam` voltage
table, and `SensorUI`/`LightUI` layout hints.

**[OBSERVED]** `Language/0-English.xml` defines the selectable functions
(`<KeyFunction>`), their sub-functions (`<SubKeyFunction>`), and guard-rail
dialogs: `<ReserveLeftKey>Must keep Left key</ReserveLeftKey>`,
`<MaxComboKey>…maximum of three keys</MaxComboKey>`,
`<DebounceTips>` double-click warning, macro insert events
(`0x400000` Key Press, `0x800000` Key Release, `0xFF0000` Delay).

## 3. Managed data model (from .NET metadata)

**[OBSERVED]** TypeDef/Field/Constant tables of the assembly (dumped with
`dnfile`):

```
DriverLib.FlashDataMap = mouseConfig, dpiConfig, dpiLed, ledBar, keys, shortCutKey, macroKey
DriverLib.MouseConfig  = reportRate, maxDPI, currentDPI, xSpindown, ySpindown,
                         silenceHeight(LOD), keyDebounceTime, motionSyncEnable,
                         linearCorrectionEnable, rippleControlEnable, ...
DriverLib.DPIConfig    = xDPI, yDPI, DPIex, color
DriverLib.KeyFunMap    = type, param1, param2
DriverLib.MacroContext = keyState, type, value, delay
DriverLib.UsbCommand   = ReportId, id, CommandStatus, address, command, receivedData
MaxCmdLength = 64
```

`Mouse_Drive_Beta.KeyFunctionStruct = {name, type, param1, param2, RightPull}` —
the UI list item backing each catalog entry.

**[OBSERVED]** Decoded enum constants:

```
KEY_CLASS: 0 Close,1 Mouse,2 DPI,3 ACPAN(scroll),4 Fire,5 Shortcut/Media,
           6 Macro,7 ReportRate,8 Light,9 Config,10 DPILock
MouseKey:  Left=1 Right=2 Middle=4 Back=8 Forward=16
REPORT_RATE: 1000=1 500=2 250=4 125=8 2000=16 4000=32
UsbCommandID: 1 EncryptionData … 7 WriteFlashData, 8 ReadFlashData,
              9 ClearSetting, 14 GetCurrentConfig, 15 SetCurrentConfig, 18 ReadVersionID …
```

**[OBSERVED]** Managed form/method inventory (dnfile): `FormMain` has 118
methods including `ButtonFunctionInit`, `SetButton`, `Button_Click`,
`DPIInit`, `ReadLocalMacroInit`, `Panel_CycleProcess_Init`, `XIn1Init`;
`FileManager.ConfigFile` has `SaveDeviceConfigFile`, `ReadDeviceConfigFile`,
`FlashDataMapToByte`, `ByteToFlashDataMap`;
`DriverLib.UsbServer32` wraps the native `CS_UsbServer_*` exports.

**[INFERRED]** Edit flow: user clicks a hotspot on the 432×356 diagram →
`FormMain` shows the `CustomKeyFunction` / `CustomSubKeyFunction` picker →
selection becomes a `KeyFunMap(type,param1,param2)` in `FlashDataMap.keys[]` →
`ConfigFile.FlashDataMapToByte` serializes → `UsbServer` sends
`WriteFlashData` (id 7) → persisted in onboard flash (the product advertises
onboard memory; `<customButton_Save>` and `<ResetConfig>` dialogs confirm it).

## 4. Native HID protocol engine (strings / exports)

**[OBSERVED]** The carved `HIDUsb.dll` (32/64-bit) exports and imports
`HID.DLL` (`HidD_SetFeature`, `HidD_GetAttributes`, `HidD_GetHidGuid`),
`SETUPAPI.dll` (`SetupDiGetClassDevsW`, `SetupDiEnumDeviceInterfaces`,
`SetupDiGetDeviceInterfaceDetailW`), `ReadFile`/`WriteFile`, and the internal
symbols:

```
UsbServer_WriteCommand, UsbServer_InitWriteCommand, UsbServer_ReadConfig,
UsbServer_ReadAllFlashData, UsbServer_ReadFalshData, UsbServer_ReadReportRate,
UsbServer_ReadDPILed, UsbServer_ReadVersion, UsbServer_SetVidPid,
CS_SetDllProtocolData, CS_ProtocolDataParser, CS_ProtocolDataUpdate,
CS_ProtocolDataCompareUpdate, CalculateChecksum, MouseConfigParser,
BufferToKeyFunMap, BufferToMacroKey, BufferToShortcutKey,
MacroKeyToBuffer, ShortcutKeyToBuffer, ByteToFlashDataMap,
CS_AESEncrypt/CS_AESDecrypt, CS_SetPassward1/2
```

Device matching string: `vid_%ls&pid_%ls&mi_%02d&col%02d` and
`vid_%ls&pid_%ls`. Battery format string `BatteryValue_%02x%02x%02x`.

**[OBSERVED]** `Config.ini` base64 tokens decode to vendor string IDs:

```
VID=Fantech3554   M_PID=FantechF536   D_PID=FantechF535,F537
CID=Fantech Helios II13   Sensor=3395
MM=CX52850P   DM=CX52650N   D2M=CH32V305   (MCU / dongle part names)
Interfaceid=1   Deviceid=5
```

**[UNVERIFIED]** The exact HID feature report framing: report id, 64-byte
**[OBSERVED — CAPTURE-CONFIRMED 2026-11-09]** The wire framing was recovered
from the 29 USBPcap captures in `captures/` (the user supplied them and the
mouse is connected). See `docs/PROTOCOL.md` for the full derivation:

* SET_REPORT(Feature), report id 8, `wValue=0x0208`, `wIndex=1`, 17 bytes,
  on interface 1; device replies on interrupt-IN endpoint `0x82`.
* 17-byte layout: `[0]=0x08`, `[1]=cmd`, `[3..4]=16-bit big-endian flash
  address`, `[5]=length`, `[6..]=data`, `[16]=checksum`.
* Checksum `= (0x55 - sum(bytes)) & 0xFF`, verified **243/243** outer and
  **153/153** inner checksums. This is the native `CalculateChecksum`.
* Flash map for report rate, LOD, DPI, debounce, button `KeyFunMap`,
  motion sync / angle snapping / ripple / peak / sensor mode derived by
  diffing one-action captures.
* Writes need no AES handshake (confirmed by `poll-125`, `dpi-0400`,
  `lod-low`, `button-remap`); the `CS_AESEncrypt` 0x01 challenge is only for
  the startup config read.
* Live hardware verification: `helios-editor probe` on the connected
  `3554:f535` returned `08030000000100b50a4900000000000041` (online), and a
  confirmed `WriteFlashData` frame for 1000 Hz was accepted with the device
  staying online.

**CORRECTION (2026-11-09, second pass):** the initial "writes work" claim was
wrong. A bare cmd-7 report is silently dropped by a cold device; the earlier
probe only showed the device stayed online, which is not an acknowledgement.
Two extra requirements were found and implemented:

* The **startup unlock** (two fixed cmd-1 password words + PCDriverStatus(1)
  + polls, from `startup-handshake.pcapng`). These are `CS_SetPassward1/2`
  constants, not an AES challenge — no `AES.dll` key is needed.
* A **poll clock**: the firmware only services config commands when a cmd-3
  `DeviceOnLine` poll is interleaved (verified live: cmd-7 echoed 10/10 with
  polls, 0/10 without). This is what the vendor's `CS_UsbServer_Thread` does.

With both, **read-back works**: after the unlock, cmd-8 returns a 10-byte
sliding flash window; each field is a 2-byte `(value, checksum)` record.
End-to-end verification on the connected mouse: `helios-editor probe` reads
`report_rate=0x01, lod=0x01, debounce=0x08`; the web UI (`--real`) applied
`8/2/12`, and a subsequent `probe` read back `0x08/0x02/0x0c`. Restored to
`1/1/8`. No remaining protocol gap for the implemented features.

## 5. What was rebuilt

`src/helios_editor/` reproduces the *editor feature*, not the vendor binary:

* 6 image-mapped physical buttons on a 432×356 mouse diagram
  (`model.py`, `web/mouse.svg`); 4 DPI stages with unique LED colors.
* The full function/sub-function catalog with type/param values (`catalog.py`).
* Validation guard rails (left button reserved, DPI range/step, report-rate
  enum, LOD enum, debounce 0–20 ms) (`model.py`).
* Profile import/export/restore and a settings editor (`server.py`, `web/*`).
* The capture-confirmed wire protocol (`protocol.py`): 17-byte feature reports,
  0x55 checksum, 16-bit address, WriteFlashData/ReadFlashData records.
  All 243 captured frames reproduced byte-for-byte.
* A real hidraw transport (`hardware.py`) using `HIDIOCSFEATURE`, wired to the
  connected `3554:f535` device; `--real` performs writes, `--dry-run` builds
  reports without ioctl.
* `SimulatedTransport` keeps the whole UI testable without hardware.

## 6. Confidence

| Claim | Confidence |
|---|---|
| 6 physical buttons on 432×356 diagram | High (config + PNG + owner correction) |
| 4 DPI stages + per-stage LED colors | High (DPIMaxGrade, DPIColor, flash sweep) |
| Function catalog type/param values | High (XML + .NET enums) |
| On-device model = 6 KeyFunMaps at 0x60+4*i | High (flash sweep) |
| HID feature reports via `WriteFlashData` (id 7) | High (exports + command enum) |
| Exact wire bytes / framing | **High** — 243/243 captured frames reproduced |
| Flash field map (rate, LOD, DPI, buttons, sensors) | High (one-action capture diffs) |
| Live writes + read-back on connected device | Confirmed (UI Apply -> `probe` read-back) |
| Startup unlock / poll clock | Confirmed + implemented (no AES key needed) |
| Runtime behavior of the vendor app | Unobserved (static + capture only) |
