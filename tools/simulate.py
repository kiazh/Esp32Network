#!/usr/bin/env python3
"""
NRF24 Link Monitor — full-pipeline simulation (no hardware required).

Simulates end-to-end:
  1. Model training      (synthetic data → sklearn DecisionTree, same as train script)
  2. NRF24L01 RX stream  (NORMAL / WEAK / INTERFERENCE packet generator)
  3. Feature extraction  (mirrors C code in link_features.c exactly)
  4. Model inference     (same decision tree as deployed on ESP32)
  5. Live dashboard      (OLED layout + serial log in terminal)

The simulation auto-cycles through all three link conditions every 15 seconds.
Press Ctrl+C to stop.

Usage:
  python3 tools/nrf24_simulate.py
  python3 tools/nrf24_simulate.py --cycle 20    # 20-second phase duration
  python3 tools/nrf24_simulate.py --no-color     # plain text output
"""

import argparse
import math
import os
import random
import sys
import threading
import time
from collections import deque
from datetime import datetime, timedelta
from typing import Optional

try:
    import numpy as np
    from sklearn.tree import DecisionTreeClassifier
except ImportError:
    sys.exit("ERROR: run  pip install numpy scikit-learn  first")

# ── ANSI colours ──────────────────────────────────────────────────────────────

USE_COLOR = True

def _c(code): return code if USE_COLOR else ''

RESET  = _c('\033[0m')
BOLD   = _c('\033[1m')
DIM    = _c('\033[2m')
GREEN  = _c('\033[92m')
YELLOW = _c('\033[93m')
RED    = _c('\033[91m')
CYAN   = _c('\033[96m')
WHITE  = _c('\033[97m')

CLASS_COLOR = {0: GREEN, 1: YELLOW, 2: RED}
CLASS_NAMES = ['NORMAL', 'WEAK', 'INTERF']
CLASS_FULL  = ['NORMAL', 'WEAK', 'INTERFERENCE']

# ── Training (identical logic to nrf24_link_monitor_train.py) ─────────────────

FEATURE_NAMES = ['pkt_rate', 'loss_rate', 'avg_iat_ms', 'burst_score']

def _gen_synthetic(n: int = 400, seed: int = 42):
    rng = np.random.default_rng(seed)
    rows, labels = [], []
    for _ in range(n):
        rows.append([rng.normal(10.0, 0.8), rng.uniform(0.00, 0.08),
                     rng.normal(100.0, 8.0), rng.uniform(0.00, 0.35)])
        labels.append(0)
    for _ in range(n):
        rows.append([rng.uniform(1.0, 5.0), rng.uniform(0.35, 0.85),
                     rng.normal(300.0, 90.0), rng.uniform(0.30, 0.95)])
        labels.append(1)
    for _ in range(n):
        rows.append([rng.normal(7.5, 2.0), rng.uniform(0.05, 0.28),
                     rng.normal(140.0, 55.0), rng.uniform(1.20, 3.00)])
        labels.append(2)
    X = np.array(rows, dtype=np.float32)
    X[:, 1] = np.clip(X[:, 1], 0.0, 1.0)
    X[:, 3] = np.clip(X[:, 3], 0.0, 3.0)
    return np.clip(X, 0, None), np.array(labels, dtype=np.int32)


def train() -> DecisionTreeClassifier:
    X, y = _gen_synthetic()
    clf = DecisionTreeClassifier(max_depth=5, min_samples_leaf=3, random_state=42)
    clf.fit(X, y)
    acc = clf.score(X, y)
    return clf, acc


# ── Feature extractor (mirrors link_features.c) ───────────────────────────────

WINDOW_SIZE = 64
WINDOW_S    = 2.0   # 2-second window

class FeatureExtractor:
    def __init__(self):
        self._buf   = deque(maxlen=WINDOW_SIZE)   # (timestamp_s, seq)
        self._total = 0
        self._lock  = threading.Lock()

    def push(self, seq: int):
        with self._lock:
            self._buf.append((time.monotonic(), seq & 0xFF))
            self._total += 1

    @property
    def total(self) -> int:
        return self._total

    def compute(self) -> Optional[dict]:
        with self._lock:
            snap = list(self._buf)

        if len(snap) < 2:
            return None

        now    = time.monotonic()
        cutoff = now - WINDOW_S

        # Find first entry inside the window
        wi = len(snap) - 1
        for i, (ts, _) in enumerate(snap):
            if ts >= cutoff:
                wi = i
                break

        win = snap[wi:]
        n   = len(win)
        if n < 2:
            return None

        # pkt_rate
        span     = win[-1][0] - win[0][0]
        pkt_rate = (n - 1) / span if span > 0.05 else 0.0

        # IAT
        iats = []
        for i in range(1, n):
            iat = (win[i][0] - win[i-1][0]) * 1000.0   # ms
            if 0.0 < iat < 5000.0:
                iats.append(iat)

        avg_iat_ms = burst_score = 0.0
        if iats:
            mean   = sum(iats) / len(iats)
            var    = sum((x - mean) ** 2 for x in iats) / len(iats)
            stddev = math.sqrt(var)
            avg_iat_ms  = mean
            burst_score = min(stddev / mean if mean > 1.0 else 0.0, 3.0)

        # loss_rate (sequence gaps)
        seq0 = win[0][1]
        seqN = win[-1][1]
        span_seq  = (seqN - seq0) % 256 + 1
        loss_rate = max(0.0, min(1.0, (span_seq - n) / span_seq)) if span_seq > n else 0.0

        return dict(pkt_rate=pkt_rate, loss_rate=loss_rate,
                    avg_iat_ms=avg_iat_ms, burst_score=burst_score)


# ── NRF24 packet stream simulator (mirrors SIM_MODE in main.c) ───────────────

class PacketStream:
    """Generates synthetic packet arrivals in a background thread."""

    def __init__(self, extractor: FeatureExtractor):
        self._fe      = extractor
        self._seq     = 0
        self._state   = 0
        self._running = False
        self._thread  = None

    def set_state(self, state: int):
        self._state = state

    def start(self):
        self._running = True
        self._thread  = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self):
        self._running = False

    def _emit(self, drop: bool = False):
        if not drop:
            self._fe.push(self._seq)
        self._seq = (self._seq + 1) & 0xFF

    def _run(self):
        while self._running:
            state = self._state

            if state == 0:   # NORMAL — regular ~10 Hz, <3% loss
                delay = max(0.001, random.gauss(0.100, 0.008))
                drop  = random.random() < 0.03
                time.sleep(delay)
                self._emit(drop)

            elif state == 1:   # WEAK — 1–4 Hz, 40–70% loss
                delay = random.uniform(0.150, 0.700)
                drop  = random.random() < 0.55
                time.sleep(delay)
                self._emit(drop)

            else:   # INTERFERENCE — bursty, moderate loss
                if random.random() < 0.22:
                    # burst: 2–5 packets in rapid succession
                    for _ in range(random.randint(2, 5)):
                        self._emit(drop=(random.random() < 0.10))
                        time.sleep(random.uniform(0.005, 0.020))
                delay = random.uniform(0.030, 0.500)
                drop  = random.random() < 0.12
                time.sleep(max(0.001, delay))
                self._emit(drop)


# ── Bar chart helper ──────────────────────────────────────────────────────────

def bar(value: float, vmin: float, vmax: float, width: int = 16) -> str:
    frac   = max(0.0, min(1.0, (value - vmin) / (vmax - vmin)))
    filled = round(frac * width)
    return '█' * filled + '░' * (width - filled)


# ── Terminal dashboard ────────────────────────────────────────────────────────

W = 52   # box inner width

def _box_line(content: str = '') -> str:
    pad = W - len(_strip_ansi(content))
    return f'║ {content}{" " * pad} ║'

def _strip_ansi(s: str) -> str:
    import re
    return re.sub(r'\033\[[0-9;]*m', '', s)

def _divider() -> str:
    return '╠' + '═' * (W + 2) + '╣'

def _top() -> str:
    return '╔' + '═' * (W + 2) + '╗'

def _bot() -> str:
    return '╚' + '═' * (W + 2) + '╝'

def _center(text: str) -> str:
    stripped = _strip_ansi(text)
    pad = (W - len(stripped)) // 2
    return ' ' * pad + text

LOG_LINES = 3

class Dashboard:
    def __init__(self):
        self._log: deque = deque(maxlen=LOG_LINES)
        self._lines_drawn = 0

    def log(self, msg: str):
        self._log.append(msg)

    def _move_up(self, n: int):
        if n > 0:
            sys.stdout.write(f'\033[{n}A\033[J')

    def draw(self, *,
             true_state: int,
             pred_state: int,
             features: Optional[dict],
             total: int,
             elapsed_s: float,
             phase_s: float,
             phase_dur: float,
             history: list):

        f = features or dict(pkt_rate=0, loss_rate=0, avg_iat_ms=0, burst_score=0)

        tc = CLASS_COLOR[true_state]
        pc = CLASS_COLOR[pred_state]
        correct = '✓' if true_state == pred_state else '✗'
        corr_c  = GREEN if true_state == pred_state else RED

        phase_pct  = min(1.0, phase_s / phase_dur)
        phase_bar  = bar(phase_pct, 0, 1, 14)
        phase_left = max(0.0, phase_dur - phase_s)

        hist_str = ' '.join(
            f"{CLASS_COLOR[p]}{CLASS_NAMES[p]}{RESET}" for p in history[-5:])

        lines = [
            _top(),
            _box_line(_center(f'{BOLD}{CYAN}NRF24 Link Monitor  —  SIMULATION{RESET}')),
            _divider(),
            _box_line(f'  True condition : {tc}{BOLD}{CLASS_FULL[true_state]:<13}{RESET}'),
            _box_line(f'  Predicted      : {pc}{BOLD}{CLASS_NAMES[pred_state]:<7}{RESET}'
                      f'  {corr_c}{correct}{RESET}'),
            _divider(),
            _box_line(f'  Rate    {f["pkt_rate"]:5.1f} p/s   '
                      f'[{bar(f["pkt_rate"], 0, 15)}]'),
            _box_line(f'  Loss   {f["loss_rate"]*100:5.1f} %      '
                      f'[{bar(f["loss_rate"], 0, 1)}]'),
            _box_line(f'  IAT    {f["avg_iat_ms"]:5.0f} ms     '
                      f'[{bar(f["avg_iat_ms"], 0, 600)}]'),
            _box_line(f'  Burst  {f["burst_score"]:5.2f}        '
                      f'[{bar(f["burst_score"], 0, 3)}]'),
            _divider(),
            _box_line(f'  Total pkts : {total:<6d}  '
                      f'Elapsed : {timedelta(seconds=int(elapsed_s))}'),
            _box_line(f'  Phase  [{phase_bar}] {phase_left:.0f}s left'),
            _box_line(f'  History: {hist_str}'),
            _divider(),
        ]
        for msg in (list(self._log) + [''] * LOG_LINES)[:LOG_LINES]:
            lines.append(_box_line(f'  {DIM}{msg[:W-2]}{RESET}'))
        lines.append(_bot())
        lines.append(f'  Auto-cycle: NORMAL → WEAK → INTERFERENCE  '
                     f'  {DIM}Ctrl+C to stop{RESET}')

        self._move_up(self._lines_drawn)
        sys.stdout.write('\n'.join(lines) + '\n')
        sys.stdout.flush()
        self._lines_drawn = len(lines)


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    global USE_COLOR

    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--cycle',    type=float, default=15.0,
                    help='Seconds per link-state phase (default: 15)')
    ap.add_argument('--no-color', action='store_true',
                    help='Disable ANSI colours')
    args = ap.parse_args()

    if args.no_color:
        USE_COLOR = False
        # re-assign after flag
        globals().update(RESET='', BOLD='', DIM='', GREEN='', YELLOW='',
                         RED='', CYAN='', WHITE='')
        CLASS_COLOR.update({0: '', 1: '', 2: ''})

    # ── Train ────────────────────────────────────────────────────────────────
    print("Training model on synthetic data ...", end=' ', flush=True)
    clf, acc = train()
    depth = clf.get_depth()
    print(f"done  (acc={acc:.0%}  depth={depth}  nodes={clf.tree_.node_count})")
    time.sleep(0.4)

    # ── Pipeline setup ───────────────────────────────────────────────────────
    feat_ext = FeatureExtractor()
    stream   = PacketStream(feat_ext)
    dash     = Dashboard()

    STATES   = [0, 1, 2]   # cycle order
    state_i  = 0
    stream.set_state(STATES[state_i])
    stream.start()

    t0         = time.monotonic()
    phase_t0   = t0
    history    = []
    last_pred  = 0
    last_feats = None

    # Initial draw (empty frame)
    print()   # breathing room

    try:
        while True:
            now          = time.monotonic()
            elapsed      = now - t0
            phase_s      = now - phase_t0

            # Auto-cycle phase
            if phase_s >= args.cycle:
                state_i  = (state_i + 1) % len(STATES)
                stream.set_state(STATES[state_i])
                phase_t0 = now
                phase_s  = 0.0

            true_state = STATES[state_i]

            # Compute features + predict
            feats = feat_ext.compute()
            if feats is not None:
                last_feats = feats
                X_row  = np.array([[feats['pkt_rate'], feats['loss_rate'],
                                    feats['avg_iat_ms'], feats['burst_score']]],
                                  dtype=np.float32)
                pred   = int(clf.predict(X_row)[0])
                last_pred = pred
                history.append(pred)

                # Serial-style log line
                ts  = datetime.now().strftime('%H:%M:%S')
                msg = (f"[{ts}] state={CLASS_NAMES[pred]:<6s} "
                       f"rate={feats['pkt_rate']:.1f} "
                       f"loss={feats['loss_rate']*100:.1f}% "
                       f"iat={feats['avg_iat_ms']:.0f}ms "
                       f"burst={feats['burst_score']:.2f}")
                dash.log(msg)

            dash.draw(
                true_state=true_state,
                pred_state=last_pred,
                features=last_feats,
                total=feat_ext.total,
                elapsed_s=elapsed,
                phase_s=phase_s,
                phase_dur=args.cycle,
                history=history,
            )

            time.sleep(1.0)   # 1 Hz update — matches firmware feat_task

    except KeyboardInterrupt:
        stream.stop()
        print(f'\n\nStopped.  Total simulated packets: {feat_ext.total}')

        # Final accuracy over history vs auto-cycle ground truth
        if history:
            # Reconstruct true labels (each phase = cycle seconds at 1 Hz)
            n      = len(history)
            phase_len = max(1, int(args.cycle))
            true_seq  = []
            for i in range(n):
                true_seq.append(STATES[(i // phase_len) % 3])

            correct = sum(p == t for p, t in zip(history, true_seq))
            print(f'Simulation accuracy : {correct}/{n} = {correct/n:.1%}')
        print()


if __name__ == '__main__':
    main()
