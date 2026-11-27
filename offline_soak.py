"""Continuously exercise a pinned working tree. Evidence survives interruption."""
import argparse
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
GATE_SECONDS = 48 * 3600


def git(*args):
    return subprocess.check_output(["git", *args], cwd=ROOT, text=True).strip()


def run(directory, smoke):
    directory = directory.resolve()
    if directory.is_relative_to(ROOT):
        raise ValueError("store soak evidence outside the source checkout")
    directory.mkdir(parents=True, exist_ok=False)
    revision = git("rev-parse","HEAD")
    started = time.monotonic()
    previous = started
    cycles = 0
    with (directory/"events.jsonl").open("x", encoding="utf-8") as events:
        def record(state, **facts):
            events.write(json.dumps(dict(utc=datetime.now(timezone.utc).isoformat(), revision=revision,
                elapsed=time.monotonic()-started, state=state, smoke=smoke, **facts))+"\n")
            events.flush()
            os.fsync(events.fileno())
        record("started")
        while True:
            current = time.monotonic()
            if current-previous > 240 or git("rev-parse","HEAD") != revision or git("status","--porcelain"):
                record("invalidated")
                return 1
            previous = current
            with (directory/f"cycle-{cycles:05d}.log").open("xb") as log:
                try:
                    result = subprocess.run([sys.executable, str(ROOT/"offline_suite.py"),
                        "test_execution_contract","test_store_contracts","test_learning_runtime",
                        "test_market_observations","test_research_admission"], cwd=ROOT, timeout=170,
                        stdout=log,stderr=subprocess.STDOUT, env={**os.environ,"PYTHONUTF8":"1"})
                except subprocess.TimeoutExpired:
                    record("timeout")
                    return 1
            cycles += 1
            record("cycle", cycle=cycles, exit_code=result.returncode)
            if result.returncode:
                return 1
            if smoke or time.monotonic()-started >= GATE_SECONDS:
                if git("rev-parse","HEAD") != revision or git("status","--porcelain"):
                    record("invalidated")
                    return 1
                record("smoke_finished" if smoke else "48h_component_soak_finished", cycles=cycles)
                return 0
            time.sleep(45)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("new_evidence_directory", type=Path)
    parser.add_argument("--smoke", action="store_true")
    args = parser.parse_args()
    raise SystemExit(run(args.new_evidence_directory,args.smoke))
