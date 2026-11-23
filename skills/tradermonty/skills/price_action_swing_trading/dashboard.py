"""Owner-friendly dashboard output module.

Provides clear, human-readable reports of trading signals, analysis, and portfolio state.
Designed to be easy to understand for owners without requiring deep trading knowledge.
"""

from typing import Dict, List, Any
from datetime import datetime


def format_trade_signal(
    symbol: str,
    signal_type: str,
    entry_price: float,
    stop_loss_price: float,
    target_price: float,
    qty: float,
    risk_amount_usd: float,
    risk_pct: float,
    reward_to_risk_ratio: float,
    confidence: str = "medium",
    pattern: str = "",
    regime: str = "neutral"
) -> str:
    """
    Format a trade signal in easy-to-understand terms for the owner.

    Example output:
        BTC/USD - BUY Signal
        Entry: $67,245.30 | Stop Loss: $65,000.00 | Target: $71,500.00
        Size: 0.05 BTC (~$3,362) | Risk: $100 (1.0%) | R:R 2:1
        Pattern: Hammer at support zone
        Market Regime: Smooth
        Confidence: MEDIUM
    """
    risk_pct_display = f"{risk_pct:.1%}"
    pnl_risk_display = f"{risk_amount_usd:.2f}"

    lines = [
        f"=== {symbol} - {signal_type.upper()} SIGNAL ===",
        f"Entry:  ${entry_price:,.2f}",
        f"Stop:   ${stop_loss_price:,.2f}",
        f"Target: ${target_price:,.2f}",
        f"Size:   {qty:.4f} {symbol.split('/')[0]} (~${qty * entry_price:,.2f})",
        f"Risk:   ${pnl_risk_display} ({risk_pct_display})",
        f"R:R:    {reward_to_risk_ratio}:1",
    ]

    if pattern:
        lines.append(f"Pattern: {pattern}")
    lines.append(f"Regime: {regime.title()}")
    lines.append(f"Confidence: {confidence.upper()}")
    lines.append("")

    return "\n".join(lines)


def format_portfolio_summary(
    positions: List[Dict[str, Any]],
    open_trades: List[Dict[str, Any]],
    daily_pnl: float,
    account_equity: float,
    regime: str = "neutral",
    recent_rules: List[str] = None
) -> str:
    """
    Format portfolio summary for owner dashboard.

    Shows current positions, open trades, daily PnL, and recent rules learned.
    """
    lines = [
        "=" * 50,
        "PORTFOLIO SUMMARY",
        "=" * 50,
        f"Date:         {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        f"Account:      ${account_equity:,.2f}",
        f"Daily PnL:    ${daily_pnl:+,.2f}",
        f"Market Regime: {regime.title()}",
        "",
        "OPEN POSITIONS:",
        "-" * 50,
    ]

    if positions:
        for pos in positions:
            lines.append(
                f"  {pos['symbol']}: {pos.get('qty', 0):.4f} @ ${pos.get('entry_price', 0):,.2f} "
                f"PnL: ${pos.get('unrealized_pnl', 0):+,.2f}"
            )
    else:
        lines.append("  No open positions")

    lines.append("")
    lines.append("OPEN TRADES (pending exit):")
    lines.append("-" * 50)

    if open_trades:
        for t in open_trades:
            lines.append(
                f"  {t['symbol']} {t['side'].upper()} {t.get('qty', 0):.4f} "
                f"status={t.get('status', 'open')}"
            )
    else:
        lines.append("  None")

    lines.append("")
    if recent_rules:
        lines.append("RECENT RULES LEARNED:")
        lines.append("-" * 50)
        for rule in recent_rules[:5]:
            lines.append(f"  • {rule}")
        lines.append("")

    lines.append("=" * 50)
    return "\n".join(lines)


def format_analysis_report(
    symbol: str,
    price_data: Dict[str, Any],
    support_resistance: Dict[str, List[Dict]],
    fibonacci: Dict[str, Any],
    volume_profile: Dict[str, Any],
    market_structure: Dict[str, Any],
    trade_signals: List[Dict[str, Any]]
) -> str:
    """
    Format complete technical analysis report for a symbol.

    Easy-to-understand summary including:
    - Current price vs key levels
    - Support/resistance zones
    - Fibonacci golden zone
    - Volume profile and POC
    - Market structure (BOS/CHoCH)
    - Trade signals
    """
    lines = [
        "=" * 60,
        f"TECHNICAL ANALYSIS REPORT - {symbol}",
        f"Generated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        "=" * 60,
        "",
    ]

    # Current price info
    if price_data:
        current_price = price_data.get('current_price', 0)
        lines.append(f"Current Price: ${current_price:,.2f}")
        lines.append("")

    # Support/Resistance zones
    lines.append("SUPPORT & RESISTANCE ZONES:")
    lines.append("-" * 60)

    support_zones = support_resistance.get('support', [])
    resistance_zones = support_resistance.get('resistance', [])

    if support_zones:
        lines.append("  Support Zones:")
        for zone in support_zones[:5]:
            price_low = zone.get('price_low', 0)
            price_high = zone.get('price_high', 0)
            conf = zone.get('confidence', 'medium')
            lines.append(
                f"    ${price_low:,.2f} - ${price_high:,.2f} (confidence: {conf})"
            )
    else:
        lines.append("  No significant support zones detected")

    lines.append("")
    if resistance_zones:
        lines.append("  Resistance Zones:")
        for zone in resistance_zones[:5]:
            price_low = zone.get('price_low', 0)
            price_high = zone.get('price_high', 0)
            conf = zone.get('confidence', 'medium')
            lines.append(
                f"    ${price_low:,.2f} - ${price_high:,.2f} (confidence: {conf})"
            )
    else:
        lines.append("  No significant resistance zones detected")

    lines.append("")

    # Fibonacci
    lines.append("FIBONACCI RETRACEMENT:")
    lines.append("-" * 60)

    if fibonacci and fibonacci.get('golden_zone'):
        gz = fibonacci['golden_zone']
        if isinstance(gz, dict):
            lines.append(
                f"  Golden Zone: ${gz.get('low', 0):,.2f} - ${gz.get('high', 0):,.2f}"
            )
            lines.append(f"  Current at golden zone: {'YES' if gz.get('current_at_zone', False) else 'NO'}")
        else:
            lines.append("  Fibonacci data unavailable")

    lines.append("")

    # Volume
    lines.append("VOLUME ANALYSIS:")
    lines.append("-" * 60)

    if volume_profile:
        poc = volume_profile.get('poc', {}).get(symbol, 0)
        imbalance = volume_profile.get('volume_imbalance', {}).get(symbol, 0)
        confirmation = volume_profile.get('confirmation', {}).get(symbol, {})

        lines.append(f"  Point of Control: ${poc:,.2f}")
        lines.append(f"  Volume Imbalance: {imbalance:+.2%} (positive = buying pressure)")

        if confirmation:
            confirmed = confirmation.get('confirmed', False)
            volume_ratio = confirmation.get('volume_ratio', 0)
            lines.append(f"  Volume Confirmation: {'YES' if confirmed else 'NO'}")
            lines.append(f"  Volume Ratio: {volume_ratio:.1f}x average")

    lines.append("")

    # Market Structure
    lines.append("MARKET STRUCTURE:")
    lines.append("-" * 60)

    if market_structure:
        bos = market_structure.get('bos', [])
        choch = market_structure.get('choch', [])

        if bos:
            lines.append(f"  Break of Structure: {len(bos)} detected")
            for b in bos[-3:]:  # show last 3
                lines.append(f"    {b.get('type', 'BOS')} at ${b.get('level', 0):,.2f}")
        else:
            lines.append("  No Break of Structure detected")

        if choch:
            lines.append(f"  Change of Character: {len(choch)} detected")
            for c in choch[-3:]:
                lines.append(f"    {c.get('type', 'CHoCH')} at ${c.get('level', 0):,.2f}")
        else:
            lines.append("  No Change of Character detected")

    lines.append("")

    # Trade Signals
    lines.append("TRADE SIGNALS:")
    lines.append("-" * 60)

    if trade_signals:
        for signal in trade_signals:
            lines.append(format_trade_signal(
                symbol=symbol,
                signal_type=signal.get('signal_type', 'buy'),
                entry_price=signal.get('entry_price', 0),
                stop_loss_price=signal.get('stop_loss_price', 0),
                target_price=signal.get('target_price', 0),
                qty=signal.get('qty', 0),
                risk_amount_usd=signal.get('risk_amount_usd', 0),
                risk_pct=signal.get('risk_pct', 0.01),
                reward_to_risk_ratio=signal.get('reward_to_risk_ratio', 2),
                confidence=signal.get('confidence', 'medium'),
                pattern=signal.get('pattern', ''),
                regime=signal.get('regime', 'neutral')
            ))
    else:
        lines.append("  No active trade signals")

    lines.append("=" * 60)
    return "\n".join(lines)