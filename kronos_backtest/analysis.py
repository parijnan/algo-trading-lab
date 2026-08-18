"""
analysis.py — Kronos Phase 1 reporting and the kill-gate verdict

Three numbers describe this strategy and none of them alone decides anything:

  * P&L per trade — what the structure earns when it is on.
  * Deployment share — how much of the calendar the single slot is occupied.
  * Return on deployed capital per unit time — the two combined.

Decision D makes the third the Phase 4 metric, because a policy that holds
longer captures more decay while foreclosing the next cycle, and a per-trade
comparison would reward it for that. Risk §7.6 is the mirror image: a policy can
also win on capital-time by occupying the slot while earning less per unit of
risk. So all three are always reported together.

The kill-gate thresholds were written into configs.py before the first run.
"""

import logging

import pandas as pd

from configs import (
    KILL_GATE_MEDIAN_PL_POSITIVE, KILL_GATE_MIN_EDGE_OVER_COSTS,
    KILL_GATE_MIN_ANNUAL_RETURN, KILL_GATE_MAX_YEAR_SHARE,
    EXIT_POLICY, EXIT_OFFSET_MODE, ENTRY_DTE_TARGET,
    SHORT_DELTA_TARGET, WING_DELTA_TARGET,
    PROFIT_TARGET_PCT_CREDIT, LOSS_MULTIPLE_CREDIT, LOT_SIZE,
)

logger = logging.getLogger(__name__)


def _capital_returns(trades: pd.DataFrame) -> dict:
    """
    Two different questions, and Decision D needs the second one.

    while_deployed — P&L over the rupee-days actually spent in trades. What the
        structure earns when it is on. Says nothing about how often it is on.

    on_committed — P&L over the rupee-days the strategy ties capital up for,
        idle days included. Margin is reserved across the whole span whether or
        not a position is open, so this is what the capital actually earns.

    They differ by roughly 1/deployment. Decision D's single-slot rule is
    precisely a statement about idle time, so Phase 4 must compare on
    `on_committed` — `while_deployed` would reward a policy for sitting out,
    which is the opposite of the intent. Both are reported so the gap is never
    hidden.
    """
    span_days = (pd.to_datetime(trades['exit_date']).max()
                 - pd.to_datetime(trades['entry_date']).min()).days
    deployed_capital_days = (trades['capital_at_risk_rs']
                             * trades['days_held'].clip(lower=1)).sum()
    # Single slot, so the reserved base is one position's worth — the largest
    # defined risk the strategy ever needs at once.
    committed_base = trades['capital_at_risk_rs'].max()
    committed_capital_days = committed_base * span_days
    total = trades['pl_rs'].sum()
    return {
        'span_days': span_days,
        'deployment': trades['days_held'].sum() / span_days if span_days else 0.0,
        'while_deployed': total / deployed_capital_days * 365.0 if deployed_capital_days else 0.0,
        'on_committed': total / committed_capital_days * 365.0 if committed_capital_days else 0.0,
        'committed_base': committed_base,
    }


def summarise(trades: pd.DataFrame, skips: pd.DataFrame, universe_size: int) -> dict:
    logger.info("Kronos Phase 1 — naive baseline")
    logger.info(f"  entry {ENTRY_DTE_TARGET} DTE | short {SHORT_DELTA_TARGET} / "
                f"wing {WING_DELTA_TARGET} | target {PROFIT_TARGET_PCT_CREDIT:.0%} of credit | "
                f"loss {LOSS_MULTIPLE_CREDIT}x | exit {EXIT_POLICY}/{EXIT_OFFSET_MODE} | "
                f"{LOT_SIZE}-lot")
    logger.info("")

    logger.info(f"  Contracts in universe : {universe_size}")
    logger.info(f"  Traded                : {len(trades)}")
    if len(skips):
        logger.info(f"  Skipped               : {len(skips)}")
        for reason, n in skips['reason'].value_counts().items():
            logger.info(f"      {reason:<28} {n:>3}")
    if trades.empty:
        logger.info("  No trades — nothing to evaluate.")
        return {'verdict': 'NO TRADES'}

    total_pl   = trades['pl_rs'].sum()
    median_pl  = trades['pl_rs'].median()
    mean_pl    = trades['pl_rs'].mean()
    slippage   = trades['slippage_cost_rs'].sum()
    gross_pl   = total_pl + slippage
    wins       = (trades['pl_rs'] > 0).sum()
    cap        = _capital_returns(trades)
    annual_ret = cap['on_committed']
    deployment = cap['deployment']

    logger.info("")
    logger.info("  P&L (1 lot)")
    logger.info(f"      total            Rs {total_pl:>12,.0f}")
    logger.info(f"      median trade     Rs {median_pl:>12,.0f}")
    logger.info(f"      mean trade       Rs {mean_pl:>12,.0f}")
    logger.info(f"      best / worst     Rs {trades['pl_rs'].max():>12,.0f} / "
                f"{trades['pl_rs'].min():,.0f}")
    logger.info(f"      win rate         {wins}/{len(trades)} = {wins/len(trades):.0%}")
    logger.info("")
    logger.info("  Costs")
    logger.info(f"      slippage paid    Rs {slippage:>12,.0f}  "
                f"(8 legs x {trades['slippage_cost_points'].iloc[0]/8:.1f} pt x {LOT_SIZE})")
    logger.info(f"      gross before it  Rs {gross_pl:>12,.0f}")
    edge_ratio = gross_pl / slippage if slippage else float('inf')
    logger.info(f"      gross / costs    {edge_ratio:>12.2f}x")
    logger.info("")
    logger.info("  Slippage sensitivity — P&L is linear in it: 8 legs x LOT_SIZE per trade")
    n = len(trades)
    breakeven = gross_pl / (8 * LOT_SIZE * n) if n else float('nan')
    for s_pt in (0.0, 0.5, 1.0, 1.5, 2.0):
        tot = gross_pl - 8 * s_pt * LOT_SIZE * n
        marker = '  <-- configured' if abs(s_pt - trades['slippage_cost_points'].iloc[0] / 8) < 1e-9 else ''
        logger.info(f"      {s_pt:>4.2f} pt/leg   total Rs {tot:>11,.0f}   "
                    f"per trade Rs {tot/n:>8,.0f}{marker}")
    logger.info(f"      breakeven at {breakeven:.2f} pt/leg — below this the strategy earns, "
                f"above it loses")
    logger.info("")
    logger.info("  Capital and time")
    logger.info(f"      median credit    {trades['credit_points'].median():>12,.1f} pts")
    logger.info(f"      median width     {trades['width_points'].median():>12,.0f} pts")
    logger.info(f"      median risk      Rs {trades['capital_at_risk_rs'].median():>12,.0f}")
    logger.info(f"      median days held {trades['days_held'].median():>12,.0f}")
    logger.info(f"      capital reserved Rs {cap['committed_base']:>12,.0f}  "
                f"(one position's defined risk — single slot)")
    logger.info(f"      deployment       {deployment:>12.0%} of the calendar")
    logger.info(f"      annualised WHILE DEPLOYED   {cap['while_deployed']:>8.1%}  "
                f"(what the structure earns when it is on)")
    logger.info(f"      annualised ON COMMITTED CAP {cap['on_committed']:>8.1%}  "
                f"(idle days included — the Phase 4 metric)")
    logger.info("")

    logger.info("  Exit reasons")
    for reason, n in trades['exit_reason'].value_counts().items():
        sub = trades[trades['exit_reason'] == reason]
        logger.info(f"      {reason:<20} {n:>3}   median Rs {sub['pl_rs'].median():>9,.0f}")
    logger.info("")

    logger.info("  Mechanics")
    logger.info(f"      deferred entries {int(trades['deferred'].sum())}/{len(trades)}")
    logger.info(f"      realised entry DTE: median {trades['entry_dte_realised'].median():.0f}, "
                f"range {trades['entry_dte_realised'].min()}-{trades['entry_dte_realised'].max()} "
                f"(target {ENTRY_DTE_TARGET})")
    logger.info(f"      liquidity substitutions fired on "
                f"{int((trades['substitutions'] > 0).sum())}/{len(trades)} trades")
    stale_share = trades['minutes_stale_skipped'].sum() / trades['minutes_in_window'].sum()
    logger.info(f"      minutes excluded as stale marks: {stale_share:.1%} "
                f"(a large share means the loss exit is not really being tested)")
    logger.info("")

    logger.info("  Year by year")
    by_year = trades.groupby(pd.to_datetime(trades['entry_date']).dt.year).agg(
        trades=('pl_rs', 'size'), total=('pl_rs', 'sum'),
        median=('pl_rs', 'median'), wins=('pl_rs', lambda s: (s > 0).sum()))
    for year, r in by_year.iterrows():
        logger.info(f"      {year}  n={int(r['trades']):>2}  "
                    f"total Rs {r['total']:>10,.0f}  median Rs {r['median']:>8,.0f}  "
                    f"wins {int(r['wins'])}/{int(r['trades'])}")

    positive = by_year[by_year['total'] > 0]['total']
    top_share = (positive.max() / total_pl) if total_pl > 0 and len(positive) else float('nan')
    logger.info(f"      best year contributes {top_share:.0%} of cumulative P&L"
                if pd.notna(top_share) else "      best-year share not meaningful (P&L <= 0)")
    logger.info("")

    return _verdict(median_pl, edge_ratio, annual_ret, top_share, total_pl)


def _verdict(median_pl, edge_ratio, annual_ret, top_share, total_pl) -> dict:
    """Apply the kill-gate criteria fixed in configs.py before the first run."""
    checks = [
        ("median trade P&L positive",
         (median_pl > 0) if KILL_GATE_MEDIAN_PL_POSITIVE else True,
         f"Rs {median_pl:,.0f}"),
        (f"gross P&L at least {KILL_GATE_MIN_EDGE_OVER_COSTS}x the slippage bill",
         edge_ratio >= KILL_GATE_MIN_EDGE_OVER_COSTS, f"{edge_ratio:.2f}x"),
        ("annualised return on committed capital positive",
         annual_ret > KILL_GATE_MIN_ANNUAL_RETURN, f"{annual_ret:.1%}"),
        (f"no year contributes more than {KILL_GATE_MAX_YEAR_SHARE:.0%} of P&L",
         bool(pd.isna(top_share)) is False and top_share <= KILL_GATE_MAX_YEAR_SHARE,
         f"{top_share:.0%}" if pd.notna(top_share) else "n/a"),
    ]
    logger.info("  Kill gate (thresholds fixed in configs.py before this run)")
    for label, ok, detail in checks:
        logger.info(f"      [{'PASS' if ok else 'FAIL'}] {label} — {detail}")

    failed = [c[0] for c in checks if not c[1]]
    verdict = 'PASS' if not failed else 'FAIL'
    logger.info("")
    logger.info(f"  VERDICT: {verdict}"
                + ("" if verdict == 'PASS' else f" — {len(failed)} criterion/criteria not met"))
    if verdict == 'FAIL':
        logger.info("  Per the plan, this is a stop-and-report, not a cue to start tuning.")
    return {'verdict': verdict, 'failed': failed, 'total_pl': total_pl,
            'median_pl': median_pl, 'edge_ratio': edge_ratio,
            'annual_return': annual_ret, 'top_year_share': top_share}
