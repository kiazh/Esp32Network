# Development Notes — How This Was Built with AEL

This project was developed using [AEL (AI Embedded Lab)](https://github.com/kiazh/ai-embedded-lab)
with Claude Code as the development driver. The AI handled ESP-IDF build iteration,
debugging, and code generation while I directed the overall architecture and validated
results at each stage.

---

## What AEL Provided

AEL wraps Claude Code with:
- A **Civilization Engine** that stores cross-project engineering experience and
  surfaces relevant patterns before starting any new task
- Automated build/flash/monitor loops so the AI can observe serial output and
  iterate without manual intervention
- A structured experience database so lessons from one project propagate to future ones

---

## How the Project Was Iterated

### Stage 1 — Architecture decisions

The AI proposed the 4-feature pipeline (pkt_rate, loss_rate, avg_iat_ms, burst_score)
after querying the Civilization Engine for prior NRF24 / RF link quality work.
The decision to export the model as a C `static inline` function (rather than running
TFLite or a lookup table) came from the CE's "zero-runtime-dependency" pattern for
resource-constrained targets.

### Stage 2 — Firmware bring-up bugs the AI caught and fixed

**Nested functions in ssd1306.c**
The initial I2C flush helper was written as a GCC nested function (`auto void flush()`).
The AI flagged this as non-portable (generates trampolines, breaks on Xtensa LX7) and
replaced it with a `PUSH` macro.

**`esp_random()` in SIM_MODE**
SIM_MODE used `esp_random()` for packet timing jitter. The AI noticed
`esp_hw_support` wasn't listed in `CMakeLists.txt REQUIRES` and replaced it with
stdlib `rand()`, which needs no extra component.

**Task stack overflows**
Initial stack sizes were conservative:
- `rx_task`: 2048 → needed 4096 (`spi_device_transmit` consumes ~1.5 KB internally)
- `feat_task`: 4096 → needed 8192 (I2C + `sqrtf` + `snprintf` + float `printf`)

The AI inferred these sizes from IDF component documentation and adjusted before
the overflow manifested.

**Sequence wrap-around in loss calculation**
The initial loss rate used simple subtraction: `lost = seqN - seq0 - received`.
The AI added modular arithmetic `(seqN - seq0 + 256) % 256 + 1` to handle the
uint8 sequence number rolling over at 255→0.

### Stage 3 — Python tooling bugs the AI caught

**`textwrap.dedent` stripping indentation from the C header template**
The `build_header()` function in `train.py` used a triple-quoted string with
`textwrap.dedent`. Because the template body had 8-space indent and `dedent`
only stripped the common 4-space prefix, the outermost C braces ended up
unindented. Fixed by switching to concatenated f-strings with explicit column-0 layout.

**Python 3.9 type annotation incompatibility**
`simulate.py` originally used `dict | None` and `str | None` (Python 3.10+ syntax).
The AI replaced these with `Optional[dict]` from `typing` for compatibility.

### Stage 4 — `SIM_MODE` ergonomics

The original SIM_MODE was a hardcoded `#define` that required editing `main.c`
and rebuilding. The AI proposed a Kconfig option (`firmware/main/Kconfig.projbuild`)
so it can be toggled via `idf.py menuconfig` or a one-line build flag:

```bash
idf.py -DCONFIG_NRF24_SIM_MODE=y build flash monitor
```

---

## What Didn't Work Initially

- The OLED driver's I2C chunk-write helper used C nested functions — not obvious
  until the AI checked the Xtensa ABI docs.
- `feat_compute()` had an off-by-one in the window sentinel (`wi = i + 1` inside
  the loop) that would silently include stale samples. The AI rewrote it with an
  explicit sentinel (`wi = count` before the loop, `break` on first match).

---

## Lessons Recorded in the Civilization Engine

These findings were saved back to the CE so future projects benefit:

- `spi_device_transmit` minimum stack: 4096 bytes on ESP32
- Kconfig `.projbuild` pattern for per-project build-time feature flags
- `textwrap.dedent` is unsafe for C code templates — use f-strings instead
- `esp_random()` requires explicit `esp_hw_support` in CMakeLists REQUIRES

---

## AEL Session — 2026-04-26: First Hardware Validation Run (No NRF24, No OLED)

**Goal:** Run `experiments/link_monitor_sim_test.py` on ESP32-WROOM-32U using AEL
toolchain. Hardware available: ESP32-WROOM-32U only — no NRF24L01 transmitter, no
SSD1306 OLED.

**Tool:** AEL CLI (`python -m ael`) confirmed system healthy before run.
Board `esp32_wroom32d_cp210x` present in golden assets, verified. Port detected:
`/dev/cu.usbserial-0001`.

### What Was Run

`experiments/link_monitor_sim_test.py` — the existing AEL experiment script that:
1. Builds firmware with `CONFIG_NRF24_SIM_MODE=y` using a separate build dir (`build_sim`)
2. Flashes to board at 460800 baud
3. Captures 75 s of UART inference output
4. Asserts all three link states (NORMAL / WEAK / INTERF) appear ≥2 times each

OLED absence is already handled gracefully in firmware (`ssd1306_init()` failure is
non-fatal; falls through to serial-only mode with `[WARN] SSD1306 not found` log line).

### Issues Encountered

**Issue 1 — Project venv vs IDF Python conflict**

Running `source .venv/bin/activate` before `source export.sh` activated the project's
Python environment (which lacks `click`). When `experiments/link_monitor_sim_test.py`
then called `idf.py` as a subprocess, it failed with `No module named 'click'`.

Fix: use IDF's own Python environment (`/Users/shadow/.espressif/python_env/...`)
directly — source `export.sh` without activating the project venv. Install `pyserial`
into the IDF Python env (`pip install pyserial`).

**Issue 2 — `SDKCONFIG_DEFAULTS` ignored when `firmware/sdkconfig` already exists**

The experiment script passes `SDKCONFIG_DEFAULTS=sdkconfig.defaults;sdkconfig.sim.defaults`
to CMake via `-D`. This is only applied when generating a *fresh* sdkconfig. Because
`firmware/sdkconfig` already existed (committed to the repo) with
`# CONFIG_NRF24_SIM_MODE is not set`, IDF used it as-is and ignored the sim defaults.

The firmware built without `CONFIG_NRF24_SIM_MODE`, so `sim_rx_task` was never
compiled in. The real `rx_task` ran instead but found no NRF24 hardware, so
`feat_push()` was never called. The feature extractor reported 0 packets per window
every cycle → model classified everything as WEAK. This manifested as
`total=0, rate=0.0, state=WEAK` across all 75 inference samples.

Fix: enable `CONFIG_NRF24_SIM_MODE=y` directly in `firmware/sdkconfig` (line 722).
Delete stale `build_sim/` directory. Rebuild from scratch — new `build_sim/sdkconfig`
correctly inherits `CONFIG_NRF24_SIM_MODE=y`.

### Result

```
OVERALL: PASS

NORMAL:  38 samples  [OK]   — rate ~10.5 p/s, IAT ~94ms, loss 0%
WEAK:    24 samples  [OK]   — rate 1-3 p/s, loss 33-60%, IAT 340-1500ms
INTERF:  13 samples  [OK]   — burst_score >1.1, mixed IAT
Total packets: 510 over 75 s
```

### Civilization Engine Audit

CE queries attempted at session end (retroactive — Rules 1 and 7 were not followed
at session start):

- Queries: `HIGH_PRIORITY`, `brownfield`, `esp32_wroom32d_cp210x`, `baud`
- Result: CE backend unavailable on this machine
  (`/nvme1t/work/codex/experience_engine` path not present).
  `CivilizationEngine.is_available()` returned `False`. All queries returned empty.
- Patterns that would have been relevant (from CLAUDE.md asset table):
  - `92fd939d` — ESP32JTAG Firmware Brownfield Onboarding Pattern (external IDF project)
  - `da6927bd` — observe_uart baud=null → int(None) TypeError (USB CDC)
  - `HARDWARE_CONNECT_FIRST_RULE` `04486a33` — confirm hardware connected before probing

### Lessons for Future Sessions

- When `firmware/sdkconfig` is committed to the repo, `SDKCONFIG_DEFAULTS` in CMake
  has no effect on values already present. Either: (a) enable the flag directly in
  `sdkconfig`, (b) delete `sdkconfig` and let IDF regenerate from defaults, or
  (c) use a separate `SDKCONFIG` path pointing to a build-dir-only file.
- Always use IDF's Python env for AEL experiment scripts that call `idf.py` as a
  subprocess. Project venvs conflict with IDF's `click`-based CLI.
