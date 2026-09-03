"""CLI tools for smartratelimit."""

import argparse
import json
import sys
from typing import Optional

from smartratelimit import RateLimiter, RateLimitStatus


def cmd_status(args):
    """Display rate limit status for endpoint(s)."""
    limiter = RateLimiter(storage=args.storage, fail_closed=getattr(args, "fail_closed", False))

    if args.endpoint:
        endpoints = [args.endpoint]
    else:
        endpoints = limiter.list_endpoints()
        if not endpoints:
            print("Error: --endpoint is required", file=sys.stderr)
            sys.exit(1)

    for endpoint in endpoints:
        status = limiter.get_status(endpoint)
        if status:
            print(f"\nEndpoint: {endpoint}")
            print(f"  Limit: {status.limit}")
            print(f"  Remaining: {status.remaining}")
            print(f"  Utilization: {status.utilization * 100:.1f}%")
            if status.reset_time:
                print(f"  Resets at: {status.reset_time}")
            if status.reset_in:
                print(f"  Resets in: {status.reset_in:.0f} seconds")
            print(f"  Exceeded: {status.is_exceeded}")
            print(f"  Confidence: {status.confidence}")

            # A caller metered on tokens as well as requests needs to see both;
            # showing only requests hides the budget that usually binds first.
            extra = [d for n, d in status.dimensions.items() if n != "requests"]
            if extra:
                print("  Other metered dimensions:")
                for dimension in extra:
                    print(
                        f"    {dimension.name}: "
                        f"{dimension.remaining}/{dimension.limit} remaining, "
                        f"window {dimension.window}, {dimension.confidence}"
                    )
            if status.confidence == "estimated":
                print(
                    "  Note: the API reported a limit but no reset time, so the "
                    "window above was assumed.\n"
                    "        Set the real one with RateLimiter.set_limit()."
                )
        else:
            print(f"\nEndpoint: {endpoint}")
            print("  No rate limit information available")


def _rate(per_second, unit=""):
    """
    Render a rate at a time unit that keeps its significant digits.

    A budget of 50 an hour is 0.83 a minute, which rounds to "0/min" and reads
    as broken. Pick the time unit that shows the number instead.

    ``unit`` names what is being counted. "50/min" against a token budget reads
    as fifty tokens a minute when it means fifty *requests* a minute, and that
    is the one number on the report someone is most likely to misread.
    """
    label = f" {unit}" if unit else ""
    per_minute = per_second * 60
    if per_minute >= 1:
        return f"{per_minute:,.0f}{label}/min"
    per_hour = per_second * 3600
    if per_hour >= 1:
        return f"{per_hour:,.0f}{label}/h"
    return f"{per_second * 86400:,.0f}{label}/day"


def _duration(seconds):
    """Render seconds at a scale a human can act on."""
    if seconds < 1:
        return f"{seconds * 1000:.0f}ms"
    if seconds < 90:
        return f"{seconds:.1f}s"
    if seconds < 5400:
        return f"{seconds / 60:.1f} min"
    return f"{seconds / 3600:.1f} h"


def cmd_clear(args):
    """Clear stored rate limit data."""
    limiter = RateLimiter(storage=args.storage, fail_closed=getattr(args, "fail_closed", False))
    limiter.clear(args.endpoint)

    if args.endpoint:
        print(f"Cleared rate limit data for: {args.endpoint}")
    else:
        print("Cleared all rate limit data")


def cmd_probe(args):
    """Probe an endpoint to detect rate limits."""
    import requests

    limiter = RateLimiter(storage=args.storage, fail_closed=getattr(args, "fail_closed", False))

    try:
        print(f"Probing {args.url}...")
        response = limiter.request("GET", args.url)

        print(f"\nResponse Status: {response.status_code}")
        print(f"\nRate Limit Headers:")
        for header in [
            "X-RateLimit-Limit",
            "X-RateLimit-Remaining",
            "X-RateLimit-Reset",
            "RateLimit-Limit",
            "RateLimit-Remaining",
            "RateLimit-Reset",
            "X-RateLimit-Limit-Tokens",
            "X-RateLimit-Remaining-Tokens",
            "X-RateLimit-Reset-Tokens",
            "x-ratelimit-limit-tokens",
            "x-ratelimit-remaining-tokens",
            "anthropic-ratelimit-tokens-limit",
            "anthropic-ratelimit-tokens-remaining",
            "Retry-After",
        ]:
            if header in response.headers:
                print(f"  {header}: {response.headers[header]}")

        status = limiter.get_status(args.url)
        if status:
            print(f"\nDetected Rate Limit:")
            print(f"  Limit: {status.limit}")
            print(f"  Remaining: {status.remaining}")
            print(f"  Window: {status.window}")
            print(f"  Confidence: {status.confidence}")
            for name, dimension in status.dimensions.items():
                if name == "requests":
                    continue
                print(
                    f"  Also metered — {name}: {dimension.limit} "
                    f"per {dimension.window} ({dimension.confidence})"
                )
            if status.confidence == "estimated":
                print(
                    "  Note: no usable reset header, so the window above is an "
                    "assumption, not a reading."
                )
        else:
            print("\nNo rate limit information detected in headers")

    except Exception as e:
        print(f"Error probing endpoint: {e}", file=sys.stderr)
        sys.exit(1)


def cmd_list(args):
    """List all tracked endpoints."""
    limiter = RateLimiter(storage=args.storage, fail_closed=getattr(args, "fail_closed", False))
    endpoints = limiter.list_endpoints()

    if not endpoints:
        print("No endpoints tracked in this storage backend.")
        print("With 'memory' storage nothing persists between commands — point")
        print("--storage at the same sqlite:// or redis:// URL your app uses.")
        return

    print(f"Tracked endpoints ({len(endpoints)}):\n")
    for endpoint in endpoints:
        status = limiter.get_status(endpoint)
        if status:
            print(
                f"  {endpoint}\n"
                f"      {status.remaining}/{status.limit} remaining, "
                f"window {status.window}, {status.confidence}"
            )
            for name, dimension in status.dimensions.items():
                if name == "requests":
                    continue
                print(
                    f"      {name}: "
                    f"{dimension.remaining}/{dimension.limit} remaining, "
                    f"window {dimension.window}, {dimension.confidence}"
                )
        else:
            print(f"  {endpoint}")


#: Shown when a --limit / --cost flag is malformed.
_FLAG_SHAPES = {
    "limit": "NAME=COUNT/WINDOW, e.g. --limit images=50/1h",
    "cost": "NAME=VALUE, e.g. --cost images=2",
}


def _parse_budget_flag(value, what):
    """Parse a ``name=...`` CLI flag into its two halves."""
    if "=" not in value:
        raise SystemExit(f"Error: --{what} expects {_FLAG_SHAPES[what]}; got {value!r}")
    name, _, rest = value.partition("=")
    return name.strip(), rest.strip()


def _build_budgets(args):
    """Turn CLI flags into the budgets the simulation runs against."""
    from datetime import timedelta

    from smartratelimit.simulate import Budget

    budgets = []

    if args.rpm:
        budgets.append(Budget("requests", args.rpm, timedelta(minutes=1), cost=1.0))
    if args.rpd:
        budgets.append(Budget("requests_daily", args.rpd, timedelta(days=1), cost=1.0))
    if args.tpm:
        budgets.append(
            Budget("tokens", args.tpm, timedelta(minutes=1), cost=args.avg_tokens)
        )

    # Anything the shorthand flags do not cover: --limit reviews=100/1h
    costs = {}
    for raw in args.cost or []:
        name, value = _parse_budget_flag(raw, "cost")
        costs[name] = float(value)

    for raw in args.limit or []:
        name, spec = _parse_budget_flag(raw, "limit")
        if "/" not in spec:
            raise SystemExit(
                f"Error: --limit expects NAME=COUNT/WINDOW, e.g. tokens=100000/1m; got {spec!r}"
            )
        count, _, window = spec.partition("/")
        try:
            limit = int(count)
        except ValueError:
            raise SystemExit(f"Error: {count!r} is not a whole number of {name!r}")

        from smartratelimit.core import RateLimiter

        try:
            window_td = RateLimiter._parse_window(window)
        except ValueError as e:
            raise SystemExit(f"Error: {e}")

        budgets.append(Budget(name, limit, window_td, cost=costs.get(name, 1.0)))

    return budgets


def cmd_simulate(args):
    """Simulate a workload against a set of rate limits."""
    from smartratelimit.simulate import SimulationError, simulate

    budgets = _build_budgets(args)
    if not budgets:
        print(
            "Error: give at least one limit — --rpm, --tpm, --rpd, or "
            "--limit NAME=COUNT/WINDOW",
            file=sys.stderr,
        )
        sys.exit(1)

    try:
        result = simulate(
            budgets=budgets,
            requests=args.requests,
            workers=args.workers,
            keys=args.keys,
            latency=args.latency,
        )
    except SimulationError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)

    if args.json:
        print(json.dumps(_result_as_dict(result), indent=2))
        return

    _print_simulation(result)


def args_latency(result):
    """Describe the per-request latency implied by the concurrency ceiling."""
    return _duration(result.workers / result.concurrency_ceiling_rps)


def _result_as_dict(result):
    """Render a SimulationResult as plain JSON-able data."""
    return {
        "requests": result.requests,
        "workers": result.workers,
        "keys": result.keys,
        "completed": result.completed,
        "wall_seconds": round(result.wall_seconds, 3),
        "achieved_rps": (
            round(result.achieved_rps, 4) if result.achieved_rps is not None else None
        ),
        "throttled": result.throttled,
        "throttled_fraction": round(result.throttled_fraction, 4),
        "mean_wait_seconds": round(result.mean_wait_seconds, 3),
        "binding": result.binding,
        "concurrency_ceiling_rps": result.concurrency_ceiling_rps,
        "burst_affected": result.burst_affected,
        "budgets": [
            {
                "name": b.name,
                "limit": b.limit,
                "window_seconds": b.window.total_seconds(),
                "cost_per_request": b.cost,
                "ceiling_per_minute": round(b.ceiling_rps * 60, 3),
                "utilisation": round(b.utilisation, 4),
                "blocked": b.blocked,
                "binding": b.binding,
            }
            for b in result.budgets
        ],
    }


#: Column widths for the budget table. Header and rows are rendered through
#: the same function so a width change cannot misalign them — hand-counting the
#: header is exactly how the columns drifted apart the first time.
_SIM_COLUMNS = (11, 20, 8, 17, 6, 9)


def _sim_row(name, raw, cost, ceiling, util, held):
    """Render one line of the budget table."""
    cells = [name.ljust(_SIM_COLUMNS[0])]
    cells += [
        value.rjust(width)
        for value, width in zip((raw, cost, ceiling, util, held), _SIM_COLUMNS[1:])
    ]
    return "  " + " ".join(cells)


def _print_simulation(result):
    """Human-readable simulation report."""
    minutes = result.wall_seconds / 60.0

    print("\nEXPECTED TRAFFIC")
    print("─" * 52)
    workers = f"{result.workers} worker" + ("" if result.workers == 1 else "s")
    keys = "" if result.keys == 1 else f" and {result.keys} keys"
    print(f"  Requests             {result.requests:,} over {workers}{keys}")
    if result.achieved_rps is None:
        print("  Wall time            instant — the whole workload fit in one burst")
    else:
        print(f"  Wall time            {_duration(result.wall_seconds)}")
        print(f"  Throughput           {result.achieved_rps:.2f} req/s"
              f"   ({_rate(result.achieved_rps, 'req')})")

    tightest_budget = min(b.ceiling_rps for b in result.budgets)
    concurrency_binds = (
        result.concurrency_ceiling_rps is not None
        and result.binding is None
        and result.concurrency_ceiling_rps < tightest_budget
    )

    print()
    print(_sim_row("BUDGET", "RAW LIMIT", "COST/REQ", "EFFECTIVE CEILING",
                   "UTIL", "HELD BACK"))
    for b in result.budgets:
        # Mark what actually constrained this run, not merely what is tightest
        # on paper — a budget can have the lowest ceiling and never bind.
        marker = "  <-- binding" if b.name == result.binding else ""
        print(
            _sim_row(
                b.name,
                # The limit as configured, in its own units: the reader needs to
                # find the number they typed somewhere on this report.
                _rate(b.limit / b.window.total_seconds(), b.name),
                f"{b.cost:,g}",
                # And what that becomes once each request's cost is paid out of
                # it — always requests, whatever the budget meters.
                _rate(b.ceiling_rps, "req"),
                f"{b.utilisation:.0%}",
                f"{b.blocked:,}",
            )
            + marker
        )

    if result.concurrency_ceiling_rps is not None:
        marker = "  <-- binding" if concurrency_binds else ""
        print(
            _sim_row(
                "concurrency",
                "—",
                "—",
                _rate(result.concurrency_ceiling_rps, "req"),
                "—",
                "—",
            )
            + marker
        )

    if result.keys > 1:
        # Otherwise the columns invite arithmetic that does not close: a 4,200
        # per-minute limit over two keys shows an 8,400 ceiling beside it.
        print(
            f"  RAW LIMIT is per key. EFFECTIVE CEILING is across all "
            f"{result.keys} keys."
        )

    print()
    if not result.completed:
        print("  ! Did not finish within the simulated horizon — the limits are far")
        print("    below this workload. Reduce the work or raise the limits.")
    elif result.binding is None:
        print("  No request was ever held back: the limits are not your constraint.")
        if concurrency_binds:
            print(
                f"    {result.workers} workers at {args_latency(result)} each caps you at "
                f"{_rate(result.concurrency_ceiling_rps, 'req')}, below every budget."
            )
    else:
        held = result.throttled_fraction
        print(f"  ! {result.binding} is the binding budget.")
        print(
            f"    {result.throttled:,} of {result.requests:,} requests ({held:.0%}) "
            f"waited, {_duration(result.mean_wait_seconds)} on average."
        )

    if result.burst_affected:
        print()
        print("  Note: buckets start full, so this run spent one bucket's worth of")
        print("  head start that will not recur. EFFECTIVE CEILING is the rate you")
        print("  can actually sustain.")

    # The line that keeps this honest.
    print()
    print("  This models YOUR limiter, not the provider's. It says when")
    print("  smartratelimit will hold a request back — not how many 429s you will")
    print("  see, which also depends on burstiness, undocumented burst allowances,")
    print("  other clients on the same key, and token estimates being estimates.")
    print()


def main():
    """Main CLI entry point."""
    parser = argparse.ArgumentParser(
        description="smartratelimit CLI tools",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )

    parser.add_argument(
        "--storage",
        default="memory",
        help="Storage backend (memory, sqlite:///path, redis://host:port)",
    )

    parser.add_argument(
        "--fail-closed",
        action="store_true",
        help=(
            "Error out if the storage backend is unreachable instead of "
            "silently falling back to in-memory state"
        ),
    )

    subparsers = parser.add_subparsers(dest="command", help="Command to run")

    # Status command
    status_parser = subparsers.add_parser("status", help="Show rate limit status")
    status_parser.add_argument(
        "endpoint",
        nargs="?",
        help=(
            "Endpoint URL, domain, or domain plus path prefix "
            "(e.g. api.example.com/search). Omit to show every tracked endpoint."
        ),
    )
    status_parser.set_defaults(func=cmd_status)

    # Clear command
    clear_parser = subparsers.add_parser("clear", help="Clear rate limit data")
    clear_parser.add_argument(
        "endpoint", nargs="?", help="Endpoint URL or domain (optional, clears all if omitted)"
    )
    clear_parser.set_defaults(func=cmd_clear)

    # Probe command
    probe_parser = subparsers.add_parser("probe", help="Probe endpoint for rate limits")
    probe_parser.add_argument("url", help="URL to probe")
    probe_parser.set_defaults(func=cmd_probe)

    # List command
    list_parser = subparsers.add_parser("list", help="List tracked endpoints")
    list_parser.set_defaults(func=cmd_list)

    # Simulate command
    sim_parser = subparsers.add_parser(
        "simulate",
        help="Model a workload against rate limits without sending anything",
        description=(
            "Run a workload through the library's own token buckets on a virtual "
            "clock and report what the limiter would do. Sends no requests."
        ),
    )
    sim_parser.add_argument("--rpm", type=int, help="Requests per minute allowed")
    sim_parser.add_argument("--tpm", type=int, help="Tokens per minute allowed")
    sim_parser.add_argument("--rpd", type=int, help="Requests per day allowed")
    sim_parser.add_argument(
        "--limit",
        action="append",
        metavar="NAME=COUNT/WINDOW",
        help="Any other budget, e.g. --limit images=50/1h (repeatable)",
    )
    sim_parser.add_argument(
        "--cost",
        action="append",
        metavar="NAME=VALUE",
        help="What one request spends of a --limit budget (default 1)",
    )
    sim_parser.add_argument(
        "--requests", type=int, default=1000, help="Requests to push through"
    )
    sim_parser.add_argument(
        "--workers", type=int, default=1, help="Concurrent workers"
    )
    sim_parser.add_argument(
        "--avg-tokens",
        type=float,
        default=1000.0,
        help="Average tokens per request, spent against --tpm",
    )
    sim_parser.add_argument(
        "--keys",
        type=int,
        default=1,
        help="API keys to spread across; each gets its own budgets",
    )
    sim_parser.add_argument(
        "--latency",
        type=float,
        default=0.0,
        help=(
            "Seconds a request occupies its worker. Set this to your API's "
            "response time to see whether concurrency or the limit binds first."
        ),
    )
    sim_parser.add_argument(
        "--json", action="store_true", help="Emit machine-readable JSON"
    )
    sim_parser.set_defaults(func=cmd_simulate)

    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        sys.exit(1)

    args.func(args)


if __name__ == "__main__":
    main()

