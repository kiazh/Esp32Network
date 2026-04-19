#!/usr/bin/env python3
"""
NRF24 Link Monitor — automated simulation test.

Builds firmware with CONFIG_NRF24_SIM_MODE=y, flashes to ESP32-WROOM-32D,
reads UART for 75 s, and asserts all three link states were predicted at
least twice each (NORMAL / WEAK / INTERFERENCE).

Usage
-----
  pip install pyserial

  # Auto-detect CP210x port:
  python3 experiments/link_monitor_sim_test.py

  # Explicit port:
  NRF24_PORT=/dev/ttyUSB0 python3 experiments/link_monitor_sim_test.py

  # Skip build+flash (board already running sim firmware):
  NRF24_SKIP_FLASH=1 NRF24_PORT=/dev/ttyUSB0 python3 experiments/link_monitor_sim_test.py

Notes
-----
- Sim firmware cycles NORMAL→WEAK→INTERFERENCE every 20 s, so 75 s capture
  covers all three phases with margin.
- feat_task runs at 1 Hz → expect ~75 [INFER] lines total.
"""

import glob
import os
import re
import subprocess
import sys
import time

_HERE        = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT    = os.path.dirname(_HERE)
FIRMWARE_DIR = os.path.join(REPO_ROOT, "firmware")
BUILD_DIR    = os.path.join(REPO_ROOT, "build_sim")

CAPTURE_SECS  = 75
MIN_PER_STATE = 2

INFER_RE = re.compile(
    r"\[INFER\]\s+state=(\S+)\s+rate=\s*(\S+)\s+loss=\s*(\S+)%\s+"
    r"iat=\s*(\S+)ms\s+burst=(\S+)"
)


def find_cp210x_port() -> str | None:
    # macOS: CP210x always appears as cu.SLAB_USBtoUART
    for tty in sorted(glob.glob("/dev/cu.SLAB_USBtoUART*")):
        return tty
    # Linux: verify VID 10c4 (Silicon Labs) via sysfs
    for tty in sorted(glob.glob("/dev/ttyUSB*")):
        base = os.path.basename(tty)
        cur  = os.path.realpath(f"/sys/class/tty/{base}/device")
        for _ in range(7):
            idv = os.path.join(cur, "idVendor")
            if os.path.exists(idv):
                try:
                    if open(idv).read().strip() == "10c4":
                        return tty
                except OSError:
                    pass
                break
            cur = os.path.dirname(cur)
    # macOS fallback for other USB-serial adapters
    for tty in sorted(glob.glob("/dev/cu.usbserial*")):
        return tty
    return None


def build_sim() -> None:
    print("[BUILD] Building firmware (SIM_MODE=y) …")
    os.makedirs(BUILD_DIR, exist_ok=True)
    defaults = os.path.join(FIRMWARE_DIR, "sdkconfig.defaults")
    sim_def  = os.path.join(FIRMWARE_DIR, "sdkconfig.sim.defaults")
    cmd = [
        "idf.py", "-C", FIRMWARE_DIR, "-B", BUILD_DIR,
        f"-DSDKCONFIG_DEFAULTS={defaults};{sim_def}",
        "build",
    ]
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        print(r.stdout[-4000:])
        print(r.stderr[-4000:])
        raise RuntimeError("Build FAILED")
    print("[BUILD] OK")


def flash(port: str) -> None:
    print(f"[FLASH] Flashing to {port} …")
    cmd = [
        "idf.py", "-C", FIRMWARE_DIR, "-B", BUILD_DIR,
        "-p", port, "-b", "460800", "flash",
    ]
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        print(r.stdout[-4000:])
        print(r.stderr[-4000:])
        raise RuntimeError("Flash FAILED")
    print("[FLASH] OK")


def reset_board(port: str) -> None:
    import serial as _serial
    s = _serial.Serial(port, 115200, timeout=0.1, rtscts=False, dsrdtr=False)
    s.setDTR(False)
    s.setRTS(True)
    time.sleep(0.12)
    s.setRTS(False)
    s.close()


def collect_infer_lines(port: str, duration_s: float) -> list[str]:
    import serial as _serial
    lines = []
    try:
        s = _serial.Serial(port, 115200, timeout=0.2, rtscts=False, dsrdtr=False)
        deadline = time.time() + duration_s
        buf = b""
        while time.time() < deadline:
            chunk = s.read(512)
            if chunk:
                buf += chunk
                while b"\n" in buf:
                    raw, buf = buf.split(b"\n", 1)
                    text = raw.decode("utf-8", errors="replace").rstrip("\r")
                    if text:
                        print(f"  {text}")
                        if "[INFER]" in text:
                            lines.append(text)
        s.close()
    except Exception as exc:
        print(f"[UART] error: {exc}")
    return lines


def main() -> int:
    port       = os.environ.get("NRF24_PORT")
    skip_flash = os.environ.get("NRF24_SKIP_FLASH", "0") == "1"

    if not skip_flash:
        build_sim()

    if not port:
        port = find_cp210x_port()
    if not port:
        print("[ERROR] No CP210x port found. Set NRF24_PORT=/dev/ttyUSBx")
        return 2
    print(f"[PORT] {port}")

    if not skip_flash:
        flash(port)
        time.sleep(2.0)
        print("[RESET] Issuing normal-boot reset …")
        reset_board(port)
        time.sleep(0.5)

    print(f"\n[UART] Capturing {CAPTURE_SECS} s of inference output …")
    lines = collect_infer_lines(port, CAPTURE_SECS)

    counts: dict[str, int] = {"NORMAL": 0, "WEAK": 0, "INTERF": 0}
    samples = []
    for line in lines:
        m = INFER_RE.search(line)
        if m:
            state = m.group(1)
            if state in counts:
                counts[state] += 1
            samples.append({
                "state":    state,
                "pkt_rate": float(m.group(2)),
                "loss_pct": float(m.group(3)),
                "iat_ms":   float(m.group(4)),
                "burst":    float(m.group(5)),
            })

    print(f"\n{'='*55}")
    print(f"  Captured {len(samples)} inference samples over {CAPTURE_SECS} s")
    print(f"  State counts: {counts}")
    print(f"{'='*55}")

    passed = True
    for state, cnt in counts.items():
        ok = cnt >= MIN_PER_STATE
        print(f"  {state:>10}: {cnt:3d} samples  {'[OK]' if ok else '[MISSING]'}")
        if not ok:
            passed = False

    print()
    if passed:
        print("OVERALL: PASS")
        return 0
    missing = [s for s, c in counts.items() if c < MIN_PER_STATE]
    print(f"OVERALL: FAIL  (states not observed: {missing})")
    return 1


if __name__ == "__main__":
    try:
        import serial  # noqa: F401
    except ImportError:
        sys.exit("ERROR: pyserial not installed. Run: pip install pyserial")
    sys.exit(main())
