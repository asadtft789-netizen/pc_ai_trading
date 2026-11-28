"""Input contracts and fencing for the one-account paper supervisor."""
import os
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path

from paper_store import stamp
from portfolio_events import Decision, MarketFrame, Quote, decision_ok, quote_ok


class WriterBusy(RuntimeError):
    pass


@contextmanager
def writer_lock(database_path):
    """Reconciliation and dispatch must agree on one serialized view of this ledger."""
    path = Path(database_path).resolve().with_suffix(".writer.lock")
    with path.open("a+b") as stream:
        if os.name == "nt":
            import msvcrt
            if path.stat().st_size == 0:
                stream.write(b"0")
                stream.flush()
            stream.seek(0)
            try:
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError as exc:
                raise WriterBusy("another supervisor owns the ledger") from exc
        else:
            import fcntl
            try:
                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise WriterBusy("another supervisor owns the ledger") from exc
        yield
        # Closing this handle releases the OS lock, including after a process exit.


def instant(ts):
    return datetime.fromisoformat(stamp(ts))


def age(ts, at=None):
    return ((at or datetime.now(timezone.utc)) - instant(ts)).total_seconds()


def fresh(q, *, entry=True, at=None):
    """Validate a raw broker quote against the revision 3 market rules."""
    try:
        bid, ask, timestamp = q
        quote = Quote("equity", instant(timestamp), bid, ask)
        return quote_ok(quote, at or datetime.now(timezone.utc), entry)
    except (TypeError, AttributeError, ValueError, InvalidOperation, OverflowError):
        return False


@dataclass(frozen=True)
class EntryEvidence:
    """Facts required from a capability-verified gateway and completed minute aggregation."""
    account: str
    symbol: str
    interval_start: str
    interval_end: str
    session_open: str
    session_close: str
    published_at: str
    decided_at: str
    clock_offset_seconds: Decimal
    minimum_notional: Decimal
    regular_session: bool
    fractionable: bool
    protection_verified: bool
    feature_ready: bool

    def permits(self, account, symbol, row, raw_quote, at, *, entry):
        if self.account != account or not account or symbol != self.symbol:
            return False
        if self.feature_ready is not True or self.regular_session is not True:
            return False
        if entry and (self.fractionable is not True or self.protection_verified is not True):
            return False
        try:
            if (not self.clock_offset_seconds.is_finite() or abs(self.clock_offset_seconds) > 2
                    or (entry and (not self.minimum_notional.is_finite() or self.minimum_notional <= 0))):
                return False
            end = instant(self.interval_end)
            if end != instant(row['t']):
                return False
            frame = MarketFrame(at, instant(self.session_open), instant(self.session_close))
            if (frame.session_close - frame.session_open).total_seconds() > 23400:
                return False
            bid, ask, timestamp = raw_quote
            quote = Quote(symbol, instant(timestamp), bid, ask)
            decision = Decision(symbol, row['strategy'], instant(self.interval_start), end,
                                instant(self.published_at), instant(self.decided_at), row['target'],
                                Decimal(str(row['atr'])) if row['atr'] is not None else Decimal(0))
            return decision_ok(decision, frame, quote, entry)
        except (ValueError, TypeError, InvalidOperation, AttributeError, OverflowError):
            return False
