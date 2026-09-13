# QSFP-DD Module Access on P150a (Custom DMC Firmware)

Reference for reading/controlling the four QSFP-DD optical cages on the Blackhole
**P150a** PCIe card from the DMC (Device Management Controller) firmware.

> **Scope / board note.** This applies to **P150a** — a single-ASIC PCIe add-in card whose
> QSFP-DD management plane hangs off the on-card **STM32 DMC**. It is **not** Galaxy. On
> Galaxy/UBB the cage plane is owned by a **BMC** and driven over IPMI
> (`syseng-blackhole-bringup/.../scripts/eth/bh_glx_cable_mgmt.py`: `ipmitool raw 0x06 0x52 …`,
> per-UBB bus → CPLD → PCA9548 mux → module `0xa0`). None of that applies here.
> ERISC firmware never touches the cages — its `QSFP_*` strings are high-speed SerDes lane maps.

---

## 1. Hardware topology (from `p150_schematic.pdf`, sheets 2, 3, 28–31, 60)

The P150a has **4 QSFP-DD cages** (A, B, C, D), each 800 Gb/s (= 2 × 400G ETH lanes).
Their **management** (not high-speed) plane is a single low-speed I2C bus:

```
STM32 DMC  "MCU_I2C0"  ==  Zephyr &i2c1
  ├─ ina228   @0x40      (board power monitor — unrelated)
  ├─ max6639  @0x2c      (fan/temp — unrelated)
  ├─ TCA9554  @0x38      QSFP-DD cage A  sideband expander
  ├─ TCA9554  @0x39      QSFP-DD cage B  sideband expander
  ├─ TCA9554  @0x3a      QSFP-DD cage C  sideband expander
  ├─ TCA9554  @0x3b      QSFP-DD cage D  sideband expander
  └─ module   @0x50      CMIS management EEPROM  — SHARED by all 4 cages
```

Two distinct things share this one bus:

- **Per-cage sideband** — one **TCA9554** (PCA9554-compatible, 8-bit GPIO expander) per cage.
  Address strapped by `A2 A1 A0`: `000→0x38 (A)`, `001→0x39 (B)`, `010→0x3a (C)`, `011→0x3b (D)`.
  Runs on `P3V3_ALWAYS_ON`; `INT#` pins aggregate to `MCU_I2C0_SMBA`.
- **Module CMIS data** — every module answers at the fixed CMIS address **`0x50`** (`0xa0` in
  8-bit notation). There is **no I2C mux**: all four modules sit on the same bus, and the cage
  whose **ModSelL is asserted (low)** is the one that responds at `0x50`.

### TCA9554 bit map (identical for all four cages)

Derived from schematic IC pin numbers (P0=pin4, P1=pin5, P2=pin6, P3=pin7, P4=pin9):

| Bit | Signal   | Dir    | Active | Meaning |
|-----|----------|--------|--------|---------|
| P0  | MODPRSL  | input  | low    | low = module seated (host-side pull-up) |
| P1  | RESETL   | output | low    | drive low = hold module in reset |
| P2  | MODSELL  | output | low    | drive low = select this cage onto the `0x50` bus |
| P3  | LPMODE   | output | high   | high = low-power mode |
| P4  | INTL     | input  | low    | open-drain interrupt from module |
| P5–P7 | unused | —      | —      | — |

Power rails: `P3V3_QSFP` / `P12V_QSFP` (sheet 60).

---

## 2. CMIS access primitives

- **Module address:** always `0x50` — mandated by SFF-8024 / CMIS, never board-specific. "Which
  cage" is the `(bus, ModSelL)` selection, never the address.
- **Identifier (byte 0):** SFF-8024 code. `0x18` = QSFP-DD (CMIS); `0x1e` = QSFP+ w/ CMIS;
  `0x0d`/`0x11` = QSFP+/QSFP28 (SFF-8636, non-CMIS); `0x19` = OSFP.
- **CMIS revision:** byte 1 (nibble.nibble, e.g. `0x50` → 5.0).
- **Paging:** the rest of memory is reached by writing the target page to the **page-select byte
  `0x7f`** (byte 127 of the lower page), then reading the offset within that page.

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
    # (CMIS inventory / app-select / DOM go here — see roadmap)
    MODSELL=1                           # release the bus for the next cage
```

**Only one cage's ModSelL may be low at a time** (shared `0x50` bus). RESETL/LPMODE are per-cage
expander bits, so those states hold independently.

---

## 3. Implementation — M1 (discovery), current

Read-only boot-time probe that logs per-cage presence + CMIS identifier.

| File | Role |
|------|------|
| `app/dmc/src/qsfp.c` | discovery logic (raw I2C on `i2c1`) |
| `app/dmc/src/qsfp.h` | declares `qsfp_discover()` |
| `app/dmc/Kconfig` | `CONFIG_TT_QSFP_DISCOVERY` (default n) |
| `app/dmc/src/main.c` | gated call to `qsfp_discover()` before the periodic timers start |
| `app/dmc/boards/tt_blackhole_tt_blackhole_dmc.conf` | enables the flag for the bench build |

**Design choices**

- **Raw I2C**, not the Zephyr `pca9554` GPIO driver: the probe *itself* answers "are the expanders
  populated?", and a genuinely absent part just logs a warning instead of throwing a driver-init
  error at boot. (The board DTS currently leaves `gpiox3-6` disabled with a stale "not detected on
  P150a" comment — the schematic shows they are populated; M2/M3 can re-enable + migrate to the driver.)
- **Boot-time + log** (no shell in the DMC build): output goes to the DMC ring-buffer log, which is
  forwarded to the SMC. Runs once, before the fan/power timers, so it has uncontended use of `i2c1`.
- Touches only `0x38–0x3b` and `0x50` — never the `ina228`/`max6639` addresses.

**Expected log**

```
QSFP-DD discovery: probing 4 cages on i2c1
QSFP A: module present, id=0x18 (QSFP-DD (CMIS))
QSFP A: CMIS rev 5.0
QSFP B: expander ok (0x39), no module seated
QSFP C: expander 0x3a not responding - cage unpopulated?
```

---

## 4. Build & flash

Build host **desktop-2**. Env under `/home/alex/mpi-shfs/tenstorrent/`:
`zephyr-venv/` (python3.12 + west) and `zephyr-sdk-1.0.1/` (arm + arc toolchains).

> **SDK version matters:** the TT zephyr-fork requires **Zephyr SDK ≥ 1.0** (use **1.0.1**). The
> `0.17.2` named in `scripts/ci-local-setup.yml` is rejected as incompatible.
>
> **Python deps beyond `requirements-base.txt`:** `cryptography intelhex click cbor2 imgtool` and
> **`grpcio-tools==1.68.0`** (pulls the nanopb-compatible **protobuf 5.29.6** — do **not** let pip
> pull protobuf 7.x, or the SMC `bh_fwtable` nanopb codegen fails with
> `KeyError: "nanopb_fileopt" is missing a containing_type`). If a build ran once with a bad
> protobuf, do a **pristine** (`-p always`) rebuild to regenerate the cached codegen.

```bash
source /home/alex/mpi-shfs/tenstorrent/zephyr-venv/bin/activate
export ZEPHYR_SDK_INSTALL_DIR=/home/alex/mpi-shfs/tenstorrent/zephyr-sdk-1.0.1
cd /home/alex/mpi-shfs/tenstorrent/tt-system-firmware

# DMC only (quick M1 compile-check):
west build -p always -b tt_blackhole@p150a/tt_blackhole/dmc app/dmc

# Full flashable bundle (SMC+DMC+mcuboot+recovery, stock ERISC):
west build --sysbuild -p always -b tt_blackhole@p150a/tt_blackhole/smc app/smc
cmake --build build --target fwbundle          # fwbundle is a SEPARATE target
#   -> build/update.fwbundle
# (for custom bring-up ERISC add: -- -DSB_CONFIG_CUSTOM_ERISC_DIR='"/home/alex/mpi-shfs/tenstorrent/bh-erisc"' \
#                                     -DSB_CONFIG_CUSTOM_ERISC_VERSION='"88.x.y"')
```

Verify M1 is baked in: `build/dmc/zephyr/.config` has `CONFIG_TT_QSFP_DISCOVERY=y`, and
`arm-zephyr-eabi-nm build/dmc/zephyr/zephyr.elf | grep qsfp_discover` shows the symbol.

**Flash (run by a human — hardware action; `sudo reboot` ends an agent session):**

```bash
tt-flash flash build/update.fwbundle --no-reset --force
sudo reboot          # fresh flash -> reboot, NOT tt-smi -r
```

Bench safety: never `peek`/`load` a wedged/in-reset core (host hard-reboots on a PCIe completion
timeout); a stock-ERISC bundle **replaces** any custom bring-up ERISC on the card.

---

## 5. Roadmap

- **M1 — discovery (done):** presence + CMIS identifier per cage, logged.
- **M2 — inventory:** CMIS page-select (`0x7f`) → decode vendor / PN / SN + Media-Type /
  applications table; expose via a DMC message or telemetry.
- **M3 — bring-up:** RESETL / LPMODE sequencing, application select via the CMIS DataPath state
  machine, DOM monitoring (temperature, Vcc, per-lane RX/TX power). Migrate sideband to the Zephyr
  `pca9554` driver and re-enable `gpiox3-6` in the DMC device tree.

## 6. References

- `Ethernet-IPs/Blackhole/p150_schematic.pdf` — sheets 2 (block), 3 (I2C tree), 28–31 (cages A–D),
  60 (QSFP power).
- SFF-8024 (identifier codes), OIF-CMIS (module memory map / paging).
- Galaxy contrast: `syseng-blackhole-bringup/.../scripts/eth/bh_glx_cable_mgmt.py`.
