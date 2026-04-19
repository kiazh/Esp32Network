#!/usr/bin/env python3
"""
NRF24 Link Monitor — model training script (AEL experiment).

Trains a decision tree on wireless link features and writes a C header
(link_model.h) that can be dropped directly into the ESP32 firmware.

Usage
-----
  # Quickstart — synthetic data only (no hardware required):
  python3 experiments/nrf24_link_monitor_train.py

  # With real collected data:
  python3 experiments/nrf24_link_monitor_train.py --data data/nrf24_link_data.csv

  # Augment sparse real data with synthetic samples:
  python3 experiments/nrf24_link_monitor_train.py --data data/nrf24_link_data.csv --augment

  # Custom tree depth or output path:
  python3 experiments/nrf24_link_monitor_train.py --depth 4 --out /tmp/link_model.h

Features (columns in CSV / model inputs)
-----------------------------------------
  pkt_rate    float   packets per second
  loss_rate   float   fraction 0.0–1.0
  avg_iat_ms  float   mean inter-arrival time (ms)
  burst_score float   IAT coefficient of variation [0, 3]

Classes
-------
  0 = NORMAL        stable, low-loss link
  1 = WEAK          high packet loss or long delays
  2 = INTERFERENCE  bursty / irregular arrival pattern
"""

import argparse
import sys
from datetime import datetime
from pathlib import Path

import numpy as np

try:
    from sklearn.tree import DecisionTreeClassifier
    from sklearn.model_selection import train_test_split
    from sklearn.metrics import classification_report, confusion_matrix
except ImportError:
    sys.exit("ERROR: scikit-learn not installed.  Run: pip install scikit-learn numpy")

# ── Constants ──────────────────────────────────────────────────────────────

FEATURE_NAMES = ['pkt_rate', 'loss_rate', 'avg_iat_ms', 'burst_score']
CLASS_NAMES   = ['NORMAL', 'WEAK', 'INTERFERENCE']

REPO_ROOT = Path(__file__).parent.parent
DEFAULT_OUT = Path(__file__).parent.parent / "firmware/main/link_model.h"


# ── Synthetic data generation ──────────────────────────────────────────────

def generate_synthetic(n_per_class: int = 400, seed: int = 42) -> tuple:
    """Generate realistic synthetic training data for each link state.

    The distributions are based on typical 2.4 GHz NRF24L01 behaviour at
    1 Mbps in a domestic environment with and without interference sources.
    """
    rng = np.random.default_rng(seed)
    rows, labels = [], []

    for _ in range(n_per_class):
        # NORMAL: regular ~10 Hz, <8% loss, ~100 ms IAT, low jitter
        rows.append([
            rng.normal(10.0, 0.8),          # pkt_rate
            rng.uniform(0.00, 0.08),         # loss_rate
            rng.normal(100.0, 8.0),          # avg_iat_ms
            rng.uniform(0.00, 0.35),         # burst_score
        ])
        labels.append(0)

    for _ in range(n_per_class):
        # WEAK: 1–5 Hz, 35–80% loss, long IAT, moderate jitter
        rows.append([
            rng.uniform(1.0, 5.0),
            rng.uniform(0.35, 0.85),
            rng.normal(300.0, 90.0),
            rng.uniform(0.30, 0.95),
        ])
        labels.append(1)

    for _ in range(n_per_class):
        # INTERFERENCE: near-normal rate, low-moderate loss, high burst
        rows.append([
            rng.normal(7.5, 2.0),
            rng.uniform(0.05, 0.28),
            rng.normal(140.0, 55.0),
            rng.uniform(1.20, 3.00),
        ])
        labels.append(2)

    X = np.array(rows, dtype=np.float32)
    y = np.array(labels, dtype=np.int32)

    # Clip to physical ranges
    X[:, 0] = np.clip(X[:, 0], 0.0, 50.0)
    X[:, 1] = np.clip(X[:, 1], 0.0,  1.0)
    X[:, 2] = np.clip(X[:, 2], 0.0, 2000.0)
    X[:, 3] = np.clip(X[:, 3], 0.0,  3.0)
    return X, y


# ── CSV loader ─────────────────────────────────────────────────────────────

def load_csv(path: Path) -> tuple:
    """Load collected data.

    Accepts lines produced by the ESP32 firmware in COLLECT mode:
        DATA,<pkt_rate>,<loss_rate>,<avg_iat_ms>,<burst_score>,<label>

    Also accepts plain CSV with a header row:
        pkt_rate,loss_rate,avg_iat_ms,burst_score,label
    """
    rows, labels = [], []
    with open(path) as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith('#'):
                continue
            # Firmware format
            if line.startswith('DATA,'):
                parts = line.split(',')
            else:
                parts = line.split(',')
            # Skip header rows
            try:
                float(parts[0] if not line.startswith('DATA,') else parts[1])
            except ValueError:
                continue
            try:
                if line.startswith('DATA,'):
                    feat = [float(parts[i]) for i in range(1, 5)]
                    lbl  = int(parts[5])
                else:
                    feat = [float(parts[i]) for i in range(4)]
                    lbl  = int(parts[4])
                rows.append(feat)
                labels.append(lbl)
            except (ValueError, IndexError):
                continue
    return np.array(rows, dtype=np.float32), np.array(labels, dtype=np.int32)


# ── Decision tree → C header ───────────────────────────────────────────────

def _tree_to_c(clf, indent: int = 1) -> str:
    """Recursively convert a fitted DecisionTreeClassifier to C if-else code."""
    tree = clf.tree_
    pad  = "    "

    def recurse(node: int, depth: int) -> str:
        sp = pad * depth
        if tree.feature[node] != -2:
            feat = FEATURE_NAMES[tree.feature[node]]
            thr  = tree.threshold[node]
            lo   = recurse(tree.children_left[node],  depth + 1)
            hi   = recurse(tree.children_right[node], depth + 1)
            return (f"{sp}if ({feat} <= {thr:.4f}f) {{\n{lo}"
                    f"{sp}}} else {{\n{hi}{sp}}}\n")
        cls = int(tree.value[node].argmax())
        return f"{sp}return {cls}; /* {CLASS_NAMES[cls]} */\n"

    return recurse(0, indent)


def build_header(clf, accuracy: float, n_samples: int) -> str:
    # body lines are already indented by _tree_to_c (depth=1 → 4-space indent)
    body = _tree_to_c(clf).rstrip()
    ts   = datetime.now().strftime('%Y-%m-%d %H:%M')
    return (
        f"/* AUTO-GENERATED by experiments/nrf24_link_monitor_train.py\n"
        f" * Generated   : {ts}\n"
        f" * Test accuracy: {accuracy:.1%}\n"
        f" * Samples      : {n_samples}\n"
        f" * Tree depth   : {clf.get_depth()}\n"
        f" * Tree nodes   : {clf.tree_.node_count}\n"
        f" *\n"
        f" * Features:\n"
        f" *   pkt_rate    — packets per second\n"
        f" *   loss_rate   — fraction missed, 0.0–1.0\n"
        f" *   avg_iat_ms  — mean inter-arrival time (ms)\n"
        f" *   burst_score — IAT coeff. of variation, 0–3\n"
        f" *\n"
        f" * Classes: 0=NORMAL  1=WEAK  2=INTERFERENCE\n"
        f" */\n"
        f"#pragma once\n"
        f"#include <stdint.h>\n"
        f"\n"
        f"#define LINK_NORMAL       0\n"
        f"#define LINK_WEAK         1\n"
        f"#define LINK_INTERFERENCE 2\n"
        f"\n"
        f'static const char * const LINK_STATE_STR[] = {{"NORMAL", "WEAK", "INTERF"}};\n'
        f"\n"
        f"static inline int model_predict(float pkt_rate, float loss_rate,\n"
        f"                                 float avg_iat_ms, float burst_score)\n"
        f"{{\n"
        f"{body}\n"
        f"}}\n"
    )


# ── Main ───────────────────────────────────────────────────────────────────

def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--data',    metavar='CSV',  help='Collected data CSV')
    ap.add_argument('--augment', action='store_true',
                    help='Mix synthetic samples with real data')
    ap.add_argument('--depth',   type=int, default=5,
                    help='Max decision tree depth (default: 5)')
    ap.add_argument('--out',     metavar='PATH', default=str(DEFAULT_OUT),
                    help='Output .h path')
    args = ap.parse_args()

    # ── Load data ──────────────────────────────────────────────────────────
    X_syn, y_syn = generate_synthetic()

    if args.data:
        data_path = Path(args.data)
        if not data_path.exists():
            sys.exit(f"ERROR: {data_path} not found")
        X_real, y_real = load_csv(data_path)
        print(f"Loaded {len(X_real)} real samples from {data_path}")

        class_counts = {c: int((y_real == c).sum()) for c in range(3)}
        print("  Class breakdown:", " | ".join(
            f"{CLASS_NAMES[c]}={class_counts[c]}" for c in range(3)))

        if args.augment or any(v < 50 for v in class_counts.values()):
            X = np.vstack([X_real, X_syn])
            y = np.concatenate([y_real, y_syn])
            print(f"  Augmented with {len(X_syn)} synthetic → {len(X)} total")
        else:
            X, y = X_real, y_real
    else:
        print(f"No --data supplied — using {len(X_syn)} synthetic samples")
        X, y = X_syn, y_syn

    # ── Train ──────────────────────────────────────────────────────────────
    X_tr, X_te, y_tr, y_te = train_test_split(
        X, y, test_size=0.20, stratify=y, random_state=42)

    clf = DecisionTreeClassifier(max_depth=args.depth, random_state=42,
                                 min_samples_leaf=3)
    clf.fit(X_tr, y_tr)

    train_acc = clf.score(X_tr, y_tr)
    test_acc  = clf.score(X_te, y_te)
    y_pred    = clf.predict(X_te)

    # ── Report ─────────────────────────────────────────────────────────────
    print(f"\n{'='*55}")
    print(f"  Train accuracy : {train_acc:.1%}   Test accuracy : {test_acc:.1%}")
    print(f"  Tree depth     : {clf.get_depth()}      Nodes         : {clf.tree_.node_count}")
    print(f"{'='*55}")
    print("\nClassification report:")
    print(classification_report(y_te, y_pred, target_names=CLASS_NAMES))
    print("Confusion matrix (rows=actual, cols=predicted):")
    cm = confusion_matrix(y_te, y_pred)
    header = "          " + "  ".join(f"{n:>8}" for n in CLASS_NAMES)
    print(header)
    for i, row in enumerate(cm):
        print(f"  {CLASS_NAMES[i]:>8}  " + "  ".join(f"{v:>8}" for v in row))

    print("\nFeature importances:")
    for name, imp in sorted(zip(FEATURE_NAMES, clf.feature_importances_),
                            key=lambda x: -x[1]):
        bar = '█' * int(imp * 40)
        print(f"  {name:14s} {imp:.3f}  {bar}")

    # ── Write header ───────────────────────────────────────────────────────
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(build_header(clf, test_acc, len(X)))
    print(f"\nModel written → {out}")
    print("Rebuild firmware and reflash to deploy.")


if __name__ == '__main__':
    main()
