from config import TOP_N_RESULTS


def run_dashboard(results, market_context):
    if not results:
        print("No results to display.")
        return

    spy_5d = market_context.get("SP500", {}).get("perf_5d", "N/A")
    qqq_5d = market_context.get("NASDAQ", {}).get("perf_5d", "N/A")

    print()
    print("=" * 110)
    print("  Trading Agent V2")
    print(f"  Market: {market_context['condition']}  |  SPY 5d: {spy_5d}%  |  QQQ 5d: {qqq_5d}%  |  Signals: {len(results)}")
    print("=" * 110)
    print(f"{'Symbol':<8} {'Signal':<13} {'Conf':>5} {'Price':>8} {'Pre%':>7} {'Target':>8} {'Stop':>8} "
          f"{'R/R':>5} {'RSI':>5} {'RelStr':>7} {'Phase':<13} Top Reason")
    print("-" * 110)

    for r in results[:TOP_N_RESULTS]:
        top_reason = r["reasons"][0] if r["reasons"] else "N/A"
        print(
            f"{r['symbol']:<8} {r['signal']:<13} {r['confidence']:>4}% "
            f"${r['price']:>7.2f} {r['pre_change']:>+6.1f}% "
            f"${r['price_target']:>7.2f} ${r['stop_loss']:>7.2f} "
            f"{r['rr_ratio']:>5.2f} {r['rsi']:>5.1f} "
            f"{r.get('rel_strength', 0):>+6.1f}% {r['phase']:<13} {top_reason}"
        )

    print("=" * 110)
    print()
