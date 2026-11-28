"""Disposable worker entry points for the integrated offline soak."""
import argparse
import os
from pathlib import Path
import sys
import time

from offline_suite import guard

sys.addaudithook(guard)
os.environ["PYTHON_DOTENV_DISABLED"] = "1"
os.environ.pop("PAPER_ENTRIES_ENABLED", None)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("role", choices=("supervise", "research"))
    parser.add_argument("database", type=Path)
    args = parser.parse_args()

    import pandas as pd
    from config import Settings
    from executor import StrategyExecutor
    from llm_mutator import LLMMutator
    from main import research_tick
    from paper_order_policy import writer_lock
    from paper_store import db, init, now, put
    from research_admission import MemoryAdmission
    from test_hourly import Fake, bars

    database = args.database
    if os.getenv("PHOENIX_SOAK_DATABASE") != str(database) or database.name != "soak.sqlite":
        raise SystemExit("Disposable soak database required")
    settings = Settings.load()
    with writer_lock(database, suffix=f".{args.role}.lock"):
        init(database)
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
            class Feed:
                diagnostics = {"source": "offline_fixture"}

                def get_train_test_split(self):
                    # The input is deterministic. The learning scheduler itself uses wall time.
                    frame = bars(400)
                    return frame.iloc[:300], frame.iloc[300:]

            admission = MemoryAdmission(database)
            feed, mutator = Feed(), LLMMutator(settings)
            tick = lambda: research_tick(database, settings, feed, mutator, admission)
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
