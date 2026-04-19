#!/usr/bin/env python3
"""
NRF24 Link Monitor — serial data collector.

Connects to the ESP32 over USB-UART, intercepts DATA lines produced by the
firmware in COLLECT mode, and saves them to a CSV file for training.

Usage
-----
  pip install pyserial

  # Basic — auto-detect port, append to default output file:
  python3 tools/nrf24_collect.py

  # Specify port and output:
  python3 tools/nrf24_collect.py --port /dev/ttyUSB0 --out data/my_run.csv

Labelling workflow
------------------
  1. Flash firmware, open this collector in one terminal.
  2. In a second terminal (or your serial monitor) send:
       c     → switch ESP32 to COLLECT mode
       0     → label = NORMAL       (stable link)
       1     → label = WEAK         (move transmitter far away / add obstacles)
       2     → label = INTERFERENCE (run a microwave or add BT traffic nearby)
  3. Collect ≥100 samples per class.
  4. Press Ctrl+C to stop.

CSV format (appended to --out)
-------------------------------
  pkt_rate,loss_rate,avg_iat_ms,burst_score,label
"""

import argparse
import sys
import time
from datetime import datetime
from pathlib import Path

try:
    import serial
    import serial.tools.list_ports
except ImportError:
    sys.exit("ERROR: pyserial not installed.  Run: pip install pyserial")

LABELS     = ['NORMAL', 'WEAK', 'INTERFERENCE']
CSV_HEADER = "pkt_rate,loss_rate,avg_iat_ms,burst_score,label\n"

REPO_ROOT    = Path(__file__).parent.parent
DEFAULT_OUT  = Path(__file__).parent.parent / "data" / "nrf24_link_data.csv"
BAUD         = 115200


def find_esp32_port():
    """Heuristic: return the first USB-serial port that looks like an ESP32."""
    for p in serial.tools.list_ports.comports():
        desc = (p.description or '').lower()
        vid  = p.vid or 0
        # CP210x (Silicon Labs) — common ESP32 UART bridge
        # CH340 / CH341 — common cheap UART bridge
        # Espressif USB-JTAG (esp32-s3 native) — VID 0x303A
        if vid in (0x10C4, 0x1A86, 0x303A) or 'cp210' in desc or 'ch340' in desc:
            return p.device
    ports = list(serial.tools.list_ports.comports())
    return ports[0].device if ports else None


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--port', default=None,
                    help='Serial port (auto-detected if omitted)')
    ap.add_argument('--baud', type=int, default=BAUD)
    ap.add_argument('--out',  default=str(DEFAULT_OUT),
                    help=f'Output CSV (default: {DEFAULT_OUT})')
    args = ap.parse_args()

    port = args.port or find_esp32_port()
    if not port:
        sys.exit("ERROR: no serial port found.  Use --port /dev/ttyUSBx")

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    write_header = not out.exists() or out.stat().st_size == 0

    counts = {0: 0, 1: 0, 2: 0}
    total  = 0
    t0     = time.time()

    print(f"Port   : {port} @ {args.baud}")
    print(f"Output : {out}")
    print(f"\nIn ESP32 serial terminal, send:")
    print("  c  → COLLECT mode    0/1/2 → set label")
    print("Press Ctrl+C to finish.\n")

    try:
        with serial.Serial(port, args.baud, timeout=1.0) as ser, \
             open(out, 'a') as fh:

            if write_header:
                fh.write(CSV_HEADER)

            while True:
                raw = ser.readline()
                if not raw:
                    continue

                try:
                    line = raw.decode('utf-8', errors='replace').strip()
                except Exception:
                    continue

                if line.startswith('DATA,'):
                    parts = line.split(',')
                    if len(parts) < 6:
                        continue
                    try:
                        feat  = [float(parts[i]) for i in range(1, 5)]
                        label = int(parts[5])
                    except (ValueError, IndexError):
                        continue

                    row = ','.join(f'{v:.4f}' for v in feat) + f',{label}\n'
                    fh.write(row)
                    fh.flush()

                    counts[label] = counts.get(label, 0) + 1
                    total += 1
                    elapsed = time.time() - t0

                    status = (f"\r[{datetime.now():%H:%M:%S}] "
                              f"N={counts[0]:4d} W={counts[1]:4d} "
                              f"I={counts[2]:4d} | total={total:5d} "
                              f"({elapsed/60:.1f} min)")
                    print(status, end='', flush=True)

                else:
                    # Pass through ESP32 log lines so the user can interact
                    print(f"\nESP32: {line}")

    except KeyboardInterrupt:
        elapsed = time.time() - t0
        print(f"\n\n── Collection complete ─────────────────")
        print(f"Duration  : {elapsed:.0f} s ({elapsed/60:.1f} min)")
        print(f"Total rows: {total}")
        for i, name in enumerate(LABELS):
            n = counts.get(i, 0)
            bar = '█' * (n // 5)
            print(f"  {name:>13s}: {n:5d}  {bar}")
        print(f"Saved to  : {out}")

        if any(counts.get(i, 0) < 30 for i in range(3)):
            print("\nWARNING: some classes have < 30 samples.")
            print("Use --augment in the training script to fill gaps with synthetic data.")

        print("\nNext step:")
        print("  python3 experiments/nrf24_link_monitor_train.py --data", out)


if __name__ == '__main__':
    main()
