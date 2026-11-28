"""Disposable worker entry points for the integrated offline soak."""
import argparse
from contextlib import ExitStack
import ctypes
from dataclasses import replace
from datetime import date, datetime, timezone
import os
from pathlib import Path
import signal
import sys
import time

from offline_suite import guard

sys.addaudithook(guard)
os.environ["PYTHON_DOTENV_DISABLED"] = "1"
os.environ.pop("PAPER_ENTRIES_ENABLED", None)


def bind_lifetime():
    expected_parent = int(os.environ["PHOENIX_SOAK_RUNNER_PID"])
    set_parent_death_signal = 1  # Linux PR_SET_PDEATHSIG
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.prctl(set_parent_death_signal, signal.SIGTERM, 0, 0, 0) != 0:
        raise OSError(ctypes.get_errno(), "cannot bind soak worker lifetime")
    # Cover the race where the runner died before prctl was installed.
    if os.getppid() != expected_parent:
        raise SystemExit("Soak runner is no longer alive")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("role", choices=("supervise", "research"))
    parser.add_argument("database", type=Path)
    args = parser.parse_args()

    import pandas as pd
    from config import Settings, UniverseConfig
    from executor import StrategyExecutor
    from llm_mutator import LLMMutator
    from main import research_tick
    from market_time import equity_sessions
    from paper_order_policy import writer_lock
    from paper_store import db, init, now, put, retain_wal_files
    from research_admission import MemoryAdmission
    from test_hourly import Fake

    database = args.database
    if os.getenv("PHOENIX_SOAK_DATABASE") != str(database) or database.name != "soak.sqlite":
        raise SystemExit("Disposable soak database required")
    bind_lifetime()
    settings = Settings.load()
    with ExitStack() as stack:
        stack.enter_context(writer_lock(database, suffix=f".{args.role}.lock"))
        init(database)
        stack.enter_context(retain_wal_files(database))
        if args.role == "supervise":
            class ObservationBroker(Fake):
                orders_enabled = False

                def snapshot(self):
                    if database.with_name("broker-down").exists():
                        raise TimeoutError()
                    return super().snapshot()

                def submit(self, *args):
                    raise AssertionError("soak attempted to submit an order")

            runner = StrategyExecutor(database, ObservationBroker(), settings)
            tick = runner.sync_positions
        else:
            settings = replace(settings, universe=UniverseConfig(["AAPL", "SPY"]))
            sessions = equity_sessions(date(2025, 8, 1), date(2025, 11, 29))
            opening = datetime(2025, 11, 28, 13, tzinfo=timezone.utc).timestamp()
            fixture_cuts = [end for session in sessions for end in session.cuts if end > opening]

            class Feed:
                diagnostics = {"source": "offline_fixture"}

                def __init__(self):
                    self.cut = opening
                    self.remaining = iter(fixture_cuts)
                    rows = []
                    for symbol in ("AAPL", "SPY"):
                        for session in sessions:
                            for start, end in zip((session.opened, *session.cuts[:-1]), session.cuts):
                                start = pd.Timestamp(start, unit="s", tz="UTC")
                                end = pd.Timestamp(end, unit="s", tz="UTC")
                                mark = 100 + (end.day % 11) / 5
                                rows.append((symbol, end, mark, mark + 1, mark - 1, mark + .1,
                                             1000, start, end, "XNYS_1m", end.isoformat(), "offline_fixture"))
                    self.frame = pd.DataFrame(rows, columns=(
                        "symbol", "timestamp", "open", "high", "low", "close", "volume",
                        "interval_start", "interval_end", "interval_kind", "received_at", "feed"
                    )).set_index(["symbol", "timestamp"]).sort_index()

                def get_train_test_split(self):
                    stamps = self.frame.index.get_level_values("timestamp")
                    at = pd.Timestamp(self.cut, unit="s", tz="UTC")
                    split = at - pd.Timedelta(days=14)
                    return self.frame.loc[stamps < split], self.frame.loc[(stamps >= split) & (stamps < at)]

                def advance(self):
                    end = next(self.remaining, None)
                    if end is not None and end + 15 > self.cut:
                        self.cut = end + 15

            admission = MemoryAdmission(database)
            feed, mutator = Feed(), LLMMutator(settings)

            def tick():
                research_tick(database, settings, feed, mutator, admission, learning_clock=lambda: feed.cut)
                feed.advance()
        while True:
            try:
                tick()
                with db(database, True) as connection:
                    put(connection, args.role + "_heartbeat", now())
                    put(connection, args.role + "_revision", os.environ["PHOENIX_REVISION"])
                time.sleep(2 if args.role == "supervise" else 15)
            except Exception as error:
                print(f"{args.role}_fault {type(error).__name__}", flush=True)
                raise SystemExit(1) from None


if __name__ == "__main__":
    main()
