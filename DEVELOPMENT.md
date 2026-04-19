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
