# Patch Notes

## v1.1.1 — SIM_MODE sdkconfig fix (2026-04-26)

### Bug Fixes

**`CONFIG_NRF24_SIM_MODE` not applied when `firmware/sdkconfig` present** — `firmware/sdkconfig`
`experiments/link_monitor_sim_test.py` passes `SDKCONFIG_DEFAULTS` to CMake to enable
SIM_MODE at build time. IDF ignores `SDKCONFIG_DEFAULTS` for values already present in
an existing `sdkconfig` file. Because `firmware/sdkconfig` was committed with
`# CONFIG_NRF24_SIM_MODE is not set`, the sim firmware was never actually built in SIM
mode — `sim_rx_task` was compiled out, `feat_push()` was never called, and the feature
extractor reported 0 packets per window, causing the model to output WEAK for all 75
inference samples regardless of phase.

Fixed by setting `CONFIG_NRF24_SIM_MODE=y` directly in `firmware/sdkconfig`.

### Files Changed

- `firmware/sdkconfig` — `CONFIG_NRF24_SIM_MODE` enabled (line 722)

---

## v1.1.0 — Bugfix Release

### Bug Fixes

**seq_span full-wrap collapse** — `firmware/main/link_features.c`
When the first and last packet in the feature window shared the same 8-bit sequence number (a full 256-wrap with n > 1 packets), `seq_span` was incorrectly computed as 1 instead of 256. This caused `loss_rate` to report near-zero when actual loss was high. Added a guard after the modular arithmetic formula to detect the full-wrap edge case and correct `seq_span` to 256.

**Timestamp captured outside mutex** — `firmware/main/link_features.c`
`now = esp_timer_get_time()` was called after releasing the mutex in `feat_compute()`. This created a race window where `feat_push()` could insert a packet between the snapshot copy and the cutoff calculation, producing inconsistent window boundaries. Moved the timestamp capture inside the mutex-protected section, before `xSemaphoreGive`.

**NRF24 RX FIFO not fully drained** — `firmware/main/main.c`, `firmware/main/nrf24l01.c`, `firmware/main/nrf24l01.h`
`rx_task()` read only one packet per poll cycle using `nrf24_packet_available()`, which checks the `RX_DR` status flag. After one read, `RX_DR` is cleared even if 1–2 packets remain in the 3-deep hardware FIFO. Added a `nrf24_fifo_empty()` helper that reads the `FIFO_STATUS` register, and replaced the single-read with a drain loop (capped at 3 iterations) that reads until the FIFO is empty.

**CONFIG register comment mismatch** — `firmware/main/nrf24l01.c`
The comment on `write_reg(REG_CONFIG, 0x0F)` stated `CRCO=1bit`, but register value `0x0F` has bit 2 (CRCO) set to 1, which means 2-byte CRC per the NRF24L01+ datasheet. A developer matching the comment instead of the register value would configure 1-byte CRC on the transmitter, causing all packets to be rejected. Corrected the comment to `CRCO=2byte CRC`. Register value unchanged.

### Documentation

**Single-IAT burst_score** — `firmware/main/link_features.c`
Added a comment explaining that `burst_score = 0.0` when only one IAT sample exists (`ni == 1`) is intentional behavior. The coefficient of variation is undefined for a single sample, and 0.0 is a safe default that won't trigger a false INTERFERENCE classification (which requires `burst_score > 1.0755`). No code logic changed.

### Files Changed

- `firmware/main/link_features.c` — seq_span guard, timestamp move, burst_score comment
- `firmware/main/main.c` — FIFO drain loop in rx_task
- `firmware/main/nrf24l01.c` — FIFO_STATUS register define, nrf24_fifo_empty() implementation, CONFIG comment fix
- `firmware/main/nrf24l01.h` — nrf24_fifo_empty() declaration
