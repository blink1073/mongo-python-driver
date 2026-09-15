# Copyright 2009-present MongoDB, Inc.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Measure the throughput difference between abi3 and non-abi3 `_cbson`.

PyMongo's C extensions normally use the CPython C API directly. The limited
API (abi3) replaces the C datetime calls with Python-level attribute access,
which costs some encode/decode throughput. This script measures that cost on
the `extended_bson` fixtures so we can decide whether the regression is
acceptable (< 5%).

Workflow
--------
1. Build a baseline (non-abi3) wheel and an abi3 wheel from the same tree.
2. Create two virtualenvs, one per wheel.
3. Point ``TEST_PATH`` at the ``extended_bson`` directory that contains
   ``flat_bson.json``, ``deep_bson.json`` and ``full_bson.json``.
4. Run ``--measure`` in each environment:

       python test/performance/bench_abi3.py --measure --output abi3.json

5. Compare the two result files:

       python test/performance/bench_abi3.py --compare \
           --baseline baseline.json --abi3 abi3.json

``--compare`` exits non-zero when the overall regression exceeds
``REGRESSION_LIMIT`` (5%).

Run this on a dedicated perf host (an Evergreen spawn host) because the
fixtures are small enough that machine load, CPU throttling, and other
background work dominate the measurement otherwise.
"""

from __future__ import annotations

import argparse
import glob
import json
import os
import statistics
import sys
import time
from typing import Any

from bson import decode, encode, json_util

REGRESSION_LIMIT = 0.05

# Each run holds ``target_time`` seconds of wall clock so the median is
# stable regardless of how fast the machine is.
TARGET_TIME = 1.0
REPEATS = 7


def load_documents(test_path: str) -> dict[str, Any]:
    """Load the three BSON fixtures as Python documents."""
    docs: dict[str, Any] = {}
    for name in ("flat_bson.json", "deep_bson.json", "full_bson.json"):
        path = os.path.join(test_path, name)
        if not os.path.exists(path):
            raise FileNotFoundError(f"missing fixture: {path}")
        with open(path) as f:
            docs[name] = json_util.loads(f.read())
    return docs


def calibrate(fn, target_time: float) -> int:
    """Return an iteration count that takes at least ``target_time`` seconds."""
    start = time.perf_counter()
    count = 1
    while time.perf_counter() - start < target_time:
        for _ in range(count):
            fn()
        count *= 2
    return max(count // 2, 1)


def per_call_seconds(fn, iterations: int, repeats: int) -> float:
    """Return the median seconds per operation over ``repeats`` untimed runs."""
    samples = []
    for _ in range(repeats):
        start = time.perf_counter()
        for _ in range(iterations):
            fn()
        samples.append((time.perf_counter() - start) / iterations)
    return statistics.median(samples)


def bench(documents: dict[str, Any], target_time: float, repeats: int) -> dict[str, float]:
    """Measure encode and decode throughput for each fixture in MB/s."""
    prepared = {name: {"doc": doc, "encoded": encode(doc)} for name, doc in documents.items()}
    results: dict[str, float] = {}
    for name, data in prepared.items():
        size = len(data["encoded"])
        enc_time = per_call_seconds(
            lambda: encode(data["doc"]),
            calibrate(lambda: encode(data["doc"]), target_time),
            repeats,
        )
        dec_time = per_call_seconds(
            lambda: decode(data["encoded"]),
            calibrate(lambda: decode(data["encoded"]), target_time),
            repeats,
        )
        results[f"{name}.encode"] = size / (enc_time * 1024 * 1024)
        results[f"{name}.decode"] = size / (dec_time * 1024 * 1024)
    return results


def evergreen_results(results: dict[str, float]) -> list[dict[str, Any]]:
    """Build Evergreen perf-harness result entries from measured MB/s."""
    entries = []
    for key, mb_per_sec in results.items():
        fixture, op = key.rsplit(".", 1)
        entries.append(
            {
                "info": {
                    "test_name": f"{fixture.replace('.json', '')}-{op}",
                    "args": {"source": "abi3"},
                },
                "metrics": [
                    {
                        "name": "megabytes_per_sec",
                        "type": "MEDIAN",
                        "value": mb_per_sec,
                        "metadata": {
                            "improvement_direction": "up",
                            "measurement_unit": "megabytes_per_second",
                        },
                    }
                ],
            }
        )
    return entries


def print_report(results: dict[str, float], baseline: dict[str, float]) -> float:
    """Print the comparison table and return the overall regression."""
    print(f"\n{'fixture':<34}{'baseline MB/s':>14}{'abi3 MB/s':>12}{'regression':>12}")
    print("-" * 72)
    total_base = 0.0
    total_abi3 = 0.0
    for key in sorted(results):
        base = baseline[key]
        abi3 = results[key]
        reg = (abi3 - base) / base if base else float("nan")
        total_base += base
        total_abi3 += abi3
        flag = "  <-- OVER LIMIT" if reg < -REGRESSION_LIMIT else ""
        print(f"{key:<34}{base:>14.2f}{abi3:>12.2f}{reg:>+12.2%}{flag}")
    overall = (total_abi3 - total_base) / total_base if total_base else float("nan")
    print("-" * 72)
    print(f"{'Overall (sum of MB/s)':<34}{total_base:>14.2f}{total_abi3:>12.2f}{overall:>+12.2%}")
    return overall


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--test-path",
        default=os.environ.get("TEST_PATH"),
        help="directory containing flat/deep/full_bson.json (default: $TEST_PATH)",
    )
    parser.add_argument(
        "--target-time",
        type=float,
        default=TARGET_TIME,
        help=f"seconds of wall clock per measurement (default: {TARGET_TIME})",
    )
    parser.add_argument("--repeats", type=int, default=REPEATS)
    parser.add_argument(
        "--pin",
        action="store_true",
        help="pin to the available CPU set (Unix only)",
    )

    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--measure", action="store_true", help="benchmark and write a results file")
    mode.add_argument("--compare", action="store_true", help="compare two results files")

    parser.add_argument("--output", help="results file to write when using --measure")
    parser.add_argument(
        "--evergreen",
        action="store_true",
        help="write Evergreen perf-harness JSON to $OUTPUT_FILE (or --output)",
    )
    parser.add_argument("--baseline", help="baseline results file when using --compare")
    parser.add_argument("--abi3", help="abi3 results file when using --compare")
    args = parser.parse_args()

    if args.pin and hasattr(os, "sched_setaffinity"):
        try:
            os.sched_setaffinity(0, {0})
        except (OSError, ValueError):
            pass

    if args.measure:
        if not args.test_path:
            print("error: --measure requires --test-path or TEST_PATH", file=sys.stderr)
            return 2
        print(f"C extension in use: {__import__('bson').has_c()}")
        if not __import__("bson").has_c():
            print("error: bson C extension not installed", file=sys.stderr)
            return 2
        test_path = args.test_path
        for candidate in (test_path, os.path.join(test_path, "extended_bson")):
            if glob.glob(os.path.join(candidate, "flat_bson.json")):
                test_path = candidate
                break
        documents = load_documents(test_path)
        results = bench(documents, args.target_time, args.repeats)

        # A single run can't compare abi3 to baseline; it only emits the
        # measurements. The compare step decides pass/fail.
        if args.evergreen:
            evergreen_path = args.output or os.environ.get("OUTPUT_FILE")
            if not evergreen_path:
                print("error: --evergreen requires --output or OUTPUT_FILE", file=sys.stderr)
                return 2
            with open(evergreen_path, "w") as f:
                json.dump(evergreen_results(results), f, indent=4)
            print(f"wrote Evergreen results to {evergreen_path}")
            return 0

        if not args.output:
            print("error: --measure requires --output", file=sys.stderr)
            return 2
        with open(args.output, "w") as f:
            json.dump(results, f, indent=2, sort_keys=True)
        print(f"wrote {args.output}: {len(results)} measurements")
        return 0

    if not args.baseline or not args.abi3:
        print("error: --compare requires --baseline and --abi3", file=sys.stderr)
        return 2
    with open(args.baseline) as f:
        baseline = json.load(f)
    with open(args.abi3) as f:
        abi3 = json.load(f)
    overall = print_report(abi3, baseline)
    ok = overall >= -REGRESSION_LIMIT
    print(
        f"\n{'PASS' if ok else 'FAIL'}: overall regression {overall:+.2%} "
        f"(limit -{REGRESSION_LIMIT:.0%})"
    )
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
