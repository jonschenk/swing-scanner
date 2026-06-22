"""The validated regime -> strategy router for the LIVE autonomous engine.

The engine picks BOTH the strategy and its parameters automatically from the current regime — the
human never tunes this. These are the parameters the backtester validated out-of-sample (mirrors
backtest.DEFAULT_ROUTER):
  * bull -> leader-pullback, cost-robust variant (uncapped 3R target + ADX>=30)
  * chop -> mean-reversion, selective variant (deep dips: 2.5x ATR stop; the >=4% stretch floor
            is enforced in the scanner)
  * bear -> CASH (the kill-switch; the engine sits out)

Strategy SELECTION is automatic. Strategy PARAMETERS are fixed and git-committed here — the running
app never self-tunes them. Changing them is a deliberate commit to this file (the "two Claudes" rule:
Claude Code edits the strategy from the journal evidence; the running app only ever APPLIES it).
The manual variation picker in the full app is a separate dev/research surface; the engine ignores it.
"""

VALIDATED_ROUTER: dict[str, tuple[str, dict]] = {
    "bull": ("leader_pullback", {"reward_mult": 3.0, "cap_target_at_high": False, "adx_min": 30.0}),
    "chop": ("mean_reversion", {"atr_stop_mult": 2.5, "mr_min_stretch_pct": 4.0}),
    "bear": ("cash", {}),
}


def for_regime(regime: str | None) -> tuple[str, dict]:
    """Return (strategy, param-overrides) the engine should run for `regime`. Unknown -> bull leg."""
    return VALIDATED_ROUTER.get(regime or "", VALIDATED_ROUTER["bull"])
