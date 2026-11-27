"""Portfolio constraints shared by exploratory replay, shadow and paper supervision."""
from decimal import Decimal

VERSION = "equity-long-cash-v1"
STOP_ATR = 2
REWARD_TO_RISK = 2
MAX_POSITIONS = 1


def protection_prices(filled_basis, initial_gap):
    if filled_basis <= 0 or not 0 < initial_gap < filled_basis:
        raise ValueError("invalid initial protection distance")
    return filled_basis - initial_gap, filled_basis + REWARD_TO_RISK * initial_gap


def portfolio_entry_budget(allocated, available, order_cap, position_cap):
    return max(Decimal(0), min(Decimal(1), allocated, available, order_cap, position_cap))


def allocated_targets(signals):
    """Reserve one symbol at each eligible next open across the entire aligned portfolio.

    Input is a timestamp-by-symbol panel of targets already delayed by one observed bar.
    Missing observations for a held symbol invalidate a comparison.
    """
    ordered = signals.sort_index(axis=1)
    chosen = ordered.copy()
    chosen.iloc[:, :] = 0
    held = None
    positions = {symbol: index for index, symbol in enumerate(ordered.columns)}
    for index, values in enumerate(ordered.itertuples(index=False, name=None)):
        if held is not None:
            value = values[positions[held]]
            if value != value:
                raise ValueError("missing held-instrument observation")
            if not value:
                held = None
        if held is None:
            held = next((s for s, value in zip(ordered.columns, values) if value == 1), None)
        if held is not None:
            chosen.iat[index, positions[held]] = 1
    return chosen
