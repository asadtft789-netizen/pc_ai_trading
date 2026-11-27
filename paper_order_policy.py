"""Input contracts and fencing for the one-account paper supervisor."""
import os
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path

from paper_store import stamp


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


def age(ts):
    return (datetime.now(timezone.utc) - instant(ts)).total_seconds()


def fresh(q, *, entry=True):
    """Exit triggers may use a recent orderly market even when its spread is wide."""
    try:
        bid, ask, timestamp = q
        if not (bid.is_finite() and ask.is_finite() and 0 < bid <= ask):
            return False
        if not -2 <= age(timestamp) <= 10:
            return False
        midpoint = (ask + bid) / 2
        return not entry or (ask - bid) * 10000 <= 20 * midpoint
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

    def permits(self, account, symbol, decision, quote_time):
        if (self.account != account or self.symbol != symbol or not self.account
                or any(flag is not True for flag in (self.regular_session, self.fractionable,
                                                     self.protection_verified, self.feature_ready))):
            return False
        try:
            start, end = instant(self.interval_start), instant(self.interval_end)
            published, decided = instant(self.published_at), instant(self.decided_at)
            return (
                end == instant(decision) and (end - start).total_seconds() == 3600
                and instant(self.session_open) <= start
                and (instant(self.session_close) - instant(self.session_open)).total_seconds() <= 23400
                and 0 <= (published - end).total_seconds() <= 120
                and 0 <= (decided - published).total_seconds() <= 30
                and 0 <= age(self.decided_at) and 0 <= age(decision) <= 300
                and instant(quote_time) > end and instant(quote_time) >= decided
                and age(self.session_close) < -1800
                and self.clock_offset_seconds.is_finite() and abs(self.clock_offset_seconds) <= 2
                and self.minimum_notional.is_finite() and self.minimum_notional > 0
            )
        except (ValueError, TypeError, InvalidOperation, AttributeError, OverflowError):
            return False
