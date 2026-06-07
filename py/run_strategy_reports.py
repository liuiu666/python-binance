"""Refresh strategy reports in dependency order."""
import os
import subprocess
import sys

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
PY_DIR = os.path.join(ROOT, "py")

QUICK_SCRIPTS = [
    "analyze_signal_audit.py",
    "analyze_live_backtest_gap.py",
    "analyze_live_trade_audit.py",
    "shadow_decision_report.py",
    "strategy_health_report.py",
    "strategy_decision_report.py",
]

FULL_EXTRA_SCRIPTS = [
    "search_10m_regime_filters.py",
    "search_10m_stateful_policy_filters.py",
    "search_30m_regime_filters.py",
    "analyze_regime_patterns.py",
    "validate_execution_latency.py",
    "strategy_robustness_profile.py",
    "analyze_parallel_portfolio.py",
    "analyze_queue_execution_policy.py",
    "search_dual_strategy_causal_filters.py",
    "validate_dual_strategy_candidate_stability.py",
    "optimize_portfolio_risk_filters.py",
    "validate_portfolio_filter_stability.py",
    "validate_session_filters.py",
]


def run(script):
    path = os.path.join(PY_DIR, script)
    print(f"\n=== {script} ===", flush=True)
    subprocess.run([sys.executable, path], cwd=ROOT, check=True)


def main():
    scripts = QUICK_SCRIPTS
    if "--full" in sys.argv[1:]:
        scripts = QUICK_SCRIPTS[:3] + FULL_EXTRA_SCRIPTS + QUICK_SCRIPTS[3:]
    for script in scripts:
        run(script)
    print("\nAll strategy reports refreshed.")


if __name__ == "__main__":
    main()
