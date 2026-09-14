# QSFP-DD Module Access on P150A (Custom DMC Firmware)

Reference for reading and controlling the four QSFP-DD optical cages on the
Blackhole **P150A** PCIe card from the DMC (Device Management Controller)
firmware.

> **Scope / board note.** This applies to **P150A** — a single-ASIC PCIe add-in
> card whose QSFP-DD management plane hangs off the on-card **STM32 DMC**. It is
> **not** Galaxy. On Galaxy/UBB the cage plane is owned by a **BMC** and driven
> over IPMI (`syseng-blackhole-bringup/.../scripts/eth/bh_glx_cable_mgmt.py`:
> `ipmitool raw 0x06 0x52 …`, per-UBB bus → CPLD → PCA9548 mux → module `0xa0`).
> None of that applies here.
>
> ERISC firmware never touches the cages. Its `QSFP_*` strings are high-speed
> SerDes lane maps. This interface manages module sideband and CMIS only.
> Application selection and ERISC/SerDes retraining are outside the current
> scope.

---

## 1. Hardware topology (from `p150_schematic.pdf`, sheets 2, 3, 28–32, 60)

The P150A has **4 QSFP-DD cages** (A, B, C, D), each 800 Gb/s (= 2 × 400G ETH
lanes). Their **management** (not high-speed) plane is a single low-speed I2C
bus:

```
STM32 DMC PA7/PB4 == hardware i2c3 == schematic "STM_I2C3" == "MCU_I2C0"
  │
  ├─ FXMA2102 U1 level translator (enabled by STM32 PD2 / MCU_CONN_I2C_EN)
  │
  ├─ SMC SMBus target @0x0a and board PCA9555 @0x20
  ├─ TCA9554  @0x38      QSFP-DD cage A  sideband expander
  ├─ TCA9554  @0x39      QSFP-DD cage B  sideband expander
  ├─ TCA9554  @0x3a      QSFP-DD cage C  sideband expander
  ├─ TCA9554  @0x3b      QSFP-DD cage D  sideband expander
  └─ module   @0x50      CMIS management EEPROM  — SHARED by all 4 cages
```

Two distinct things share this one bus:

- **Per-cage sideband** — one **TCA9554** (PCA9554-compatible, 8-bit GPIO
  expander) per cage. Address strapped by `A2 A1 A0`: `000→0x38 (A)`,
  `001→0x39 (B)`, `010→0x3a (C)`, `011→0x3b (D)`. Runs on `P3V3_ALWAYS_ON`;
  `INT#` pins aggregate to `MCU_I2C0_SMBA`.
- **Module CMIS data** — every module answers at the fixed CMIS address
  **`0x50`** (`0xa0` in 8-bit notation). There is **no I2C mux**: all four
  modules sit on the same bus, and the cage whose **ModSelL is asserted (low)**
  is the one that responds at `0x50`.

### TCA9554 bit map (identical for all four cages)

Derived from schematic IC pin numbers (P0=pin4, P1=pin5, P2=pin6, P3=pin7,
P4=pin9):

| Bit   | Signal  | Dir    | Active | Meaning |
|-------|---------|--------|--------|---------|
| P0    | MODPRSL | input  | low    | low = module seated (host-side pull-up) |
| P1    | RESETL  | output | low    | drive low = hold module in reset |
| P2    | MODSELL | output | low    | drive low = select this cage onto the `0x50` bus |
| P3    | LPMODE  | output | high   | high = low-power mode |
| P4    | INTL    | input  | low    | open-drain interrupt from module |
| P5–P7 | unused  | —      | —      | — |

Power rails: `P3V3_QSFP` / `P12V_QSFP` (sheet 60).

---

## 2. CMIS access primitives

- **Module address:** always `0x50` — mandated by SFF-8024 / CMIS, never
  board-specific. "Which cage" is the `(bus, ModSelL)` selection, never the
  address.
- **Identifier (byte 0):** SFF-8024 code. `0x18` = QSFP-DD (CMIS); `0x1e` =
  QSFP+ w/ CMIS; `0x0d`/`0x11` = QSFP+/QSFP28 (SFF-8636, non-CMIS); `0x19` =
  OSFP.
- **CMIS revision:** byte 1 (nibble.nibble, e.g. `0x50` → 5.0).
- **Paging:** the rest of memory is reached by writing the target page to the
  **page-select byte `0x7f`** (byte 127 of the lower page), then reading the
  offset within that page.

### TCA9554 registers

| Reg  | Name           | Notes |
|------|----------------|-------|
| 0x00 | Input Port     | read pin levels |
| 0x01 | Output Port    | set output levels |
| 0x03 | Configuration  | 1 = input, 0 = output; POR default `0xFF` (all input) |

### Per-cage sequence (select → read/act → release)

```
configure TCA9554: P0,P4 = input;  P1,P2,P3 = output
idle:  RESETL=1 (out of reset), MODSELL=1 (deselected), LPMODE per policy
present = !read(P0)                     # MODPRSL low = seated
if present:
    assert MODSELL=0 on this cage, MODSELL=1 on all others   # own the shared 0x50 bus
    id  = i2c_read(0x50, 0x00)          # expect 0x18
    rev = i2c_read(0x50, 0x01)
    # CMIS inventory / DOM / page reads go here
    MODSELL=1                           # release the bus for the next cage
```

**Only one cage's ModSelL may be low at a time** (shared `0x50` bus). RESETL
and LPMODE are per-cage expander bits, so those states hold independently.
Any number of cages may be in high power at once; an optical link needs high
power at both ends.

> **Optical airflow.** Passive copper can sit in low power indefinitely.
> Optical modules dissipate far more heat once LPMODE is deasserted. Do not
> leave an optical module in high power without chassis airflow over the cage
> faceplate.

---

## 3. Implementation

The DMC manager is split across focused units. Host, SMC, and DMC share one
protocol header.

| File | Role |
|------|------|
| `app/dmc/src/qsfp/qsfp_bus.c` / `qsfp_bus.h` | MCU_I2C0 session, FXMA2102 U1 OE, TCA9554, ModSelL, output shadows |
| `app/dmc/src/qsfp/qsfp_cmis.c` / `qsfp_cmis.h` | EEPROM at `0x50`, page select, identifier / flat-mem |
| `app/dmc/src/qsfp/qsfp_telemetry.c` | boot discovery and 1 s presence poll that pack `TAG_QSFP_STATUS` |
| `app/dmc/src/qsfp/qsfp_mgmt.c` | live host operations: status, inventory, LPMODE, RESETL, DOM, page dump |
| `app/dmc/src/qsfp/qsfp.h` | DMC app API (`qsfp_discover`, `qsfp_poll`, `qsfp_handle_mgmt`, park/translator) |
| `include/tenstorrent/qsfp_mgmt.h` | canonical protocol: telemetry bytes, ops, request packing, response/payload structs |
| `app/dmc/Kconfig` | `CONFIG_TT_QSFP` (default n). `CONFIG_TT_QSFP_DISCOVERY` is a deprecated alias |
| `app/dmc/src/main.c` | boot discovery, 1 s poll, CM2DM management dispatch, U1 off during JTAG |
| `app/dmc/boards/tt_blackhole_tt_blackhole_dmc.conf` | `CONFIG_TT_QSFP=y` for P150A DMC |
| `boards/tenstorrent/tt_blackhole/tt_blackhole_tt_blackhole_dmc.dts` | U1 OE on PD2, held low during JTAG |
| `scripts/qsfp.py` | unified host CLI |
| `scripts/qsfp_lib.py` | PCIe telemetry + `TT_SMC_MSG_QSFP_MGMT` client |
| `scripts/qsfp_telem.py` | compatibility wrapper (`telem_main`) |
| `scripts/qsfp_mgmt.py` | compatibility wrapper (`mgmt_main`) |

**Design choices**

- **Raw hardware I2C**, not the Zephyr `pca9554` GPIO driver: the probe itself
  answers "are the expanders populated?", and a genuinely absent part logs a
  warning instead of throwing a driver-init error at boot. The board DTS
  leaves `gpiox3-6` disabled; a future GPIO-driver migration must first place
  those expanders on P150A `i2c3` rather than enabling the existing nodes on
  another bus.
- Boot inventory runs before periodic traffic. Runtime presence uses short
  1-second transactions.
- Discovery keeps U1 off during JTAG, enables it afterward, and serializes
  ModSelL so exactly one module can answer at `0x50`.
- Per-cage output shadows preserve LPMODE across polling. A poll never
  silently forces a host-managed high-power cage back to low power.
- Touches only `0x38–0x3b` and `0x50`.

**Expected boot log**

```
QSFP-DD discovery: probing 4 cages on MCU_I2C0
QSFP A: QSFP-DD (CMIS) id=0x18, CMIS 5.0
QSFP B: expander 0x39 present, no module
QSFP C: expander 0x3a absent
```

### Passive telemetry vs live management

`TAG_QSFP_STATUS` (telemetry tag 81) is a **passive** packed word: one byte
per cage (A in bits 7:0). It reports expander presence, module seating, and
the SFF-8024 identifier. It does not move LPMODE or RESETL and does not
read vendor strings or DOM.

| Byte value | Meaning |
|------------|---------|
| `0x00` | expander absent |
| `0x01` | expander present, no module seated |
| `0x02` | module seated, CMIS `0x50` read failed |
| `0x03` | MCU_I2C0 stuck (discovery aborted) |
| `0x80 \| id` | module present; `id` is the seven-bit identifier |

Live control uses `TT_SMC_MSG_QSFP_MGMT`. The host request is packed into a
single CM2DM word (`QSFP_MGMT_REQUEST`). The DMC completes the I2C operation,
releases ModSelL, and returns a 28-byte `qsfp_mgmt_response` (token,
operation, cage, status, payload). The SMC matches token/op/cage so stale
replies are discarded.

| Op | Meaning |
|----|---------|
| `STATUS` | pins, identifier, revision, power mode, CMIS module state |
| `INVENTORY` | vendor / PN / SN / connector / media class / AppSel IDs |
| `SET_POWER` | move the LPMODE pin and return; poll `STATUS` for Ready |
| `RESET` | pulse RESETL |
| `DOM_MODULE` / `DOM_LANE` | temperature, Vcc, per-lane TX/RX monitors |
| `READ_PAGE` | 20-byte slice of CMIS memory (pages 00/01/02/10/11 or lower) |

Low power is the boot default and is sufficient for inventory. High power is
explicit and never automatic: DMC does not drop LPMODE just because an
optical module is seated. Lasers and DOM need `power <cage> high` with
airflow over the faceplate; a mis-detected media class or an unattended
optic can overheat. `SET_POWER` only moves the LPMODE pin; an optical
module can spend seconds in `PwrUp`, far longer than a host request may
block, so the CLI then polls `ModuleState` until it reads `Ready`.

`info` decodes SFF-8024 connector (page 00h byte 203), CMIS media class
(byte 128), and advertised AppSel host/media IDs (lower page 86–117).
Byte 128 is sometimes a second copy of Identifier (`0x18` = QSFP-DD,
`0x1e` = QSFP112); that is not SMF vs copper. When it is unusable, the
media interface table is chosen from the host electrical IDs: a BASE-CR
rate only exists on a copper assembly and an AUI only on a transceiver.
InfiniBand and Fibre Channel rates decide nothing, so they follow
whatever the rest of the module advertises.

Flat-memory modules (lower byte 2 bit 7, set on the QSFP112 DACs here)
implement lower memory and page 00h only, and some reject writes to the
page select register. Discovery skips page select on those parts and
refuses pages 01h/02h/10h/11h rather than returning page 00h under
another page's name, so `dom` and `lanes` fail cleanly on a DAC. Because
page 00h can be refused outright, `info` reports the lower-memory fields
it did read and marks the rest `unavailable` instead of failing.

`info` labels page 00h advertising. **CMIS** modules (identifier `0x18` /
`0x1e`) use power class and max watts (bytes 200–201, 0.25 W units), cable
length (byte 202, multiplier in bits 7–6), copper attenuation at 5 / 7 /
12.9 / 25.8 GHz (bytes 204–207), and media technology (byte 212). **SFF-8636**
QSFP/QSFP+/QSFP28 (`0x0c`/`0x0d`/`0x11`) use the older map: extended
identifier bits 7–6 for the 1.5/2/2.5/3.5 W class, copper length at byte
146, attenuation at 2.5 / 5 / 7 / 12.9 GHz (bytes 186–189), and device
technology nibble at byte 147. Treating an 8636 DAC as CMIS produces
garbage vendor strings and wattages. Transceivers leave length 0; DACs
that omit attenuation print nothing for that line. Use `page <cage> lower`
to dump bytes 0–127 when a module misbehaves.

CMIS lane pages 10h/11h do not exist on a CMIS flat-memory module or on any
SFF-8636 module. Firmware returns `UNAVAILABLE` for those page reads instead
of an I2C error. `SET_POWER` moves the LPMODE expander pin even if the cage
is empty. SFF-8636 keeps everything in lower memory instead:
temperature at bytes 22-23, Vcc at 26-27, four channels of RX power (34-41),
TX bias (42-49), and TX power (50-57) gated on page 00h byte 220 bit 2, lane
flags at bytes 3-5 (LOS, fault, CDR LOL, one nibble per direction), and
TxDisable at byte 86. `dom` and `lanes` read those directly, which is how a
QSFP28 optic such as `QSFP28-SR4-100G` reports power and lane flags here.
SFF-8636 has no datapath state machine and no AppSel, so `lanes` prints only
the flags, and `dom` reports the byte 2 bit 0 Data_Not_Ready bit in place of
a CMIS module state.

Passive copper such as `QDD-800G-PC005` or `QSFP-400G-PC015` reports `Ready`
in low power and implements no monitors at all: lower bytes 14–17 read zero
in either power mode, so `dom` reports temperature, Vcc, and the lane
monitors as not implemented rather than as a read failure. Optical modules
report temperature, Vcc, and per-lane TX bias/power and RX power only after
LPMODE is deasserted.

---

## 4. Host commands

Use `scripts/qsfp.py`. `--json` emits structured output; `--watch [SEC]`
repeats (default 1.0 s) on `discover`, `status`, `info`, `dom`, and `lanes`.

```bash
python3 scripts/qsfp.py discover
python3 scripts/qsfp.py discover --watch --json
python3 scripts/qsfp.py status
python3 scripts/qsfp.py status A --watch
python3 scripts/qsfp.py info A
python3 scripts/qsfp.py power A high
python3 scripts/qsfp.py power C high          # independent; both may stay high
python3 scripts/qsfp.py dom A --watch
python3 scripts/qsfp.py lanes A
python3 scripts/qsfp.py page A 11
python3 scripts/qsfp.py page A lower         # bytes 0-127, always readable
python3 scripts/qsfp.py reset A
python3 scripts/qsfp.py power A low
```

`scripts/qsfp_telem.py` and `scripts/qsfp_mgmt.py` remain as compatibility
wrappers around the same library (`discover` and the older `list`/`info`/…
subcommands). Prefer `qsfp.py`.

---

## 5. Build, test, and flash

Follow the workspace getting-started guide for Python, West, Zephyr SDK, and
blobs. From the firmware repository:

```bash
# DMC-only compile check:
west build -p always -b tt_blackhole@p150a/tt_blackhole/dmc app/dmc

# Full flashable bundle (SMC+DMC+mcuboot+recovery, stock ERISC):
west build --sysbuild -p always -b tt_blackhole@p150a/tt_blackhole/smc app/smc
cmake --build build --target fwbundle
#   -> build/update.fwbundle
```

Confirm management is baked in: `build/dmc/zephyr/.config` has
`CONFIG_TT_QSFP=y`, and
`arm-zephyr-eabi-nm build/dmc/zephyr/zephyr.elf | grep qsfp_discover` shows
the symbol.

Host-side decoder and CLI tests do not require hardware:

```bash
python3 -m pytest tests/scripts -q
```

Firmware protocol/transport coverage lives in
`tests/lib/tenstorrent/bh_arc/src/smbus_target.c`. Run the
`tests/lib/tenstorrent/bh_arc` Twister suite on `native_sim`; on-card checks are
the CLI commands above after flash.

**Flash (hardware action; a reboot ends an unattended session):**

```bash
tt-flash flash build/update.fwbundle --no-reset --force
sudo reboot          # fresh flash -> reboot, not tt-smi -r
```

Bench safety: never `peek`/`load` a wedged or in-reset core (host hard-reboots
on a PCIe completion timeout). A stock-ERISC bundle **replaces** any custom
bring-up ERISC on the card.

---

## 6. Implemented scope and follow-up

- Discovery and hotplug status are exposed through `TAG_QSFP_STATUS`.
- Inventory, LPMODE, RESETL, module/per-lane DOM, lane flags, and diagnostic
  page reads use the runtime management command.
- Application selection and ERISC SerDes/link training are outside this
  interface.
- A future GPIO-driver migration must first represent the expanders on P150A
  `i2c3`; the existing `gpiox3-6` nodes on another bus must not simply be
  enabled.

## 7. References

- `Ethernet-IPs/Blackhole/p150_schematic.pdf` — sheets 2 (block), 3 (I2C
  tree), 28–31 (cages A–D), 60 (QSFP power).
- SFF-8024 (identifier codes), OIF-CMIS (module memory map / paging),
  SFF-8636 (QSFP28 / QSFP+ inventory).
- Galaxy contrast: `syseng-blackhole-bringup/.../scripts/eth/bh_glx_cable_mgmt.py`.
