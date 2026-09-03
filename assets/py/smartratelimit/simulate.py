"""Offline simulation of a rate-limited workload.

Answers the question you would otherwise answer by deploying and watching:
*given these limits and this traffic, what actually happens?*

The simulation runs the library's own :class:`~smartratelimit.models.TokenBucket`
on a virtual clock, so an hour of traffic is modelled in milliseconds and the
answer comes from the same code that paces real requests. A separate
reimplementation would be free to disagree with the limiter -- and a simulator
that disagrees with the thing it simulates is worse than no simulator.

What it can and cannot tell you
-------------------------------

It knows exactly when **your limiter** will hold a request back, because that is
arithmetic over budgets you supply. It does **not** predict the provider's 429s:
those depend on burstiness at the provider's edge, undocumented burst
allowances, other clients sharing your key, and the gap between the token count
you estimate and the one you are billed for. Treat "throttled by your limiter"
as a fact and anything about 429s as a separate question this cannot answer.
"""

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Dict, List, Optional

from smartratelimit.models import REQUESTS, TokenBucket

#: Simulated seconds after which a run gives up. A workload that cannot finish
#: within this is already telling you what you needed to know.
DEFAULT_HORIZON = 24 * 3600.0


class SimulationError(ValueError):
    """Raised when a scenario cannot be simulated as described."""


@dataclass
class Budget:
    """One metered dimension and what a single request spends of it."""

    name: str
    limit: int
    window: timedelta
    cost: float = 1.0

    @property
    def refill_rate(self) -> float:
        """Units replenished per second."""
        return self.limit / self.window.total_seconds()

    @property
    def ceiling_rps(self) -> float:
        """
        Requests per second this budget permits once the initial burst is spent.

        A budget of 100k tokens per minute against 2k-token requests permits 50
        requests a minute however many workers you point at it.
        """
        if self.cost <= 0:
            return float("inf")
        return self.refill_rate / self.cost


@dataclass
class BudgetResult:
    """What one budget did during a run."""

    name: str
    limit: int
    window: timedelta
    cost: float
    ceiling_rps: float
    achieved_per_window: float
    utilisation: float
    #: Requests this budget, specifically, made wait.
    blocked: int
    #: True when this budget has the tightest ceiling of those configured. That
    #: is a fact about the configuration; whether it actually held anything back
    #: is ``blocked``.
    binding: bool


@dataclass
class SimulationResult:
    """The outcome of a simulated workload."""

    requests: int
    workers: int
    keys: int
    wall_seconds: float
    #: None when the workload fit inside the opening burst, so no time passed
    #: and a throughput figure would be meaningless.
    achieved_rps: Optional[float]
    throttled: int
    total_wait_seconds: float
    budgets: List[BudgetResult]
    binding: Optional[str]
    concurrency_ceiling_rps: Optional[float]
    #: True when the buckets starting full inflated the observed rate above
    #: what is sustainable. Compare the headline against ``ceiling_rps``.
    burst_affected: bool
    completed: bool = True

    @property
    def throttled_fraction(self) -> float:
        """Share of requests the limiter held back, 0.0-1.0."""
        return self.throttled / self.requests if self.requests else 0.0

    @property
    def mean_wait_seconds(self) -> float:
        """Mean wait across the requests that were held back."""
        return self.total_wait_seconds / self.throttled if self.throttled else 0.0

    def budget(self, name: str) -> Optional[BudgetResult]:
        """Look up one budget's result by name."""
        for result in self.budgets:
            if result.name == name:
                return result
        return None


def simulate(
    budgets: List[Budget],
    requests: int,
    workers: int = 1,
    keys: int = 1,
    latency: float = 0.0,
    horizon: float = DEFAULT_HORIZON,
) -> SimulationResult:
    """
    Run ``requests`` through ``budgets`` and report what happens.

    Args:
        budgets: The metered dimensions and what one request spends of each.
        requests: How many requests to push through.
        workers: Concurrent workers. With ``latency=0`` this cannot be the
            bottleneck, since a worker is free the instant its request is
            admitted.
        keys: API keys to spread across. Each key gets its own budgets, and a
            worker takes whichever key can serve it soonest.
        latency: Seconds a request occupies its worker. Set this to your API's
            typical response time to find out whether concurrency or the rate
            limit is what actually holds you up.
        horizon: Simulated seconds before giving up.

    Returns:
        A :class:`SimulationResult`.

    Raises:
        SimulationError: If the scenario is impossible as described.
    """
    if requests <= 0:
        raise SimulationError("requests must be positive")
    if workers <= 0:
        raise SimulationError("workers must be positive")
    if keys <= 0:
        raise SimulationError("keys must be positive")
    if not budgets:
        raise SimulationError("at least one budget is required")

    for budget in budgets:
        if budget.limit <= 0 or budget.window.total_seconds() <= 0:
            raise SimulationError(
                f"budget {budget.name!r} has a non-positive limit or window"
            )
        if budget.cost > budget.limit:
            # No amount of waiting fixes this: the bucket refills to `limit` and
            # stops, so a request costing more than that never becomes payable.
            raise SimulationError(
                f"a single request costs {budget.cost:g} of {budget.name!r}, but "
                f"the entire budget is {budget.limit:g} per "
                f"{_format_window(budget.window)} — no request can ever be sent. "
                f"Raise the limit or lower the per-request cost."
            )

    epoch = datetime(2000, 1, 1)
    spent = [b for b in budgets if b.cost > 0]

    # One independent set of buckets per key.
    buckets = [
        {
            b.name: TokenBucket(
                capacity=float(b.limit),
                tokens=float(b.limit),
                refill_rate=b.refill_rate,
                last_update=epoch,
            )
            for b in spent
        }
        for _ in range(keys)
    ]

    worker_free = [0.0] * workers
    blocked: Dict[str, int] = {b.name: 0 for b in spent}
    clock = 0.0
    completed = 0
    throttled = 0
    total_wait = 0.0

    while completed < requests and clock <= horizon:
        # The worker that frees up soonest takes the next request.
        w = min(range(workers), key=lambda i: worker_free[i])
        clock = max(clock, worker_free[w])

        # Then the key that can serve it soonest.
        best_key, best_wait, best_culprit = None, None, None
        for k in range(keys):
            now = epoch + timedelta(seconds=clock)
            wait, culprit = 0.0, None
            for b in spent:
                this = buckets[k][b.name].wait_time(b.cost, now=now)
                if this > wait:
                    wait, culprit = this, b.name
            if best_wait is None or wait < best_wait:
                best_key, best_wait, best_culprit = k, wait, culprit
            if wait <= 0:
                break

        if best_wait > 0:
            blocked[best_culprit] += 1
            throttled += 1
            total_wait += best_wait
            clock += best_wait

        now = epoch + timedelta(seconds=clock)
        for b in spent:
            buckets[best_key][b.name].consume(b.cost, now=now)

        completed += 1
        worker_free[w] = clock + latency

    wall = clock
    ceilings = {b.name: b.ceiling_rps * keys for b in spent}
    tightest = min(ceilings.values()) if ceilings else float("inf")

    results = []
    for b in spent:
        window_seconds = b.window.total_seconds()
        # Measure over at least one window. A workload that fits inside the
        # opening burst finishes in zero simulated time, and "requests per
        # second" over a zero-length run is not a large number, it is an
        # undefined one. Spending 40 of a 50-per-hour budget in three seconds
        # is 80% of that hour, which is the figure someone can act on.
        span = max(wall, window_seconds)
        achieved = (completed * b.cost) / (span / window_seconds)
        results.append(
            BudgetResult(
                name=b.name,
                limit=b.limit,
                window=b.window,
                cost=b.cost,
                ceiling_rps=ceilings[b.name],
                achieved_per_window=achieved,
                # Per key: the simulated total is spread across `keys` buckets.
                utilisation=(achieved / keys) / b.limit,
                blocked=blocked[b.name],
                binding=ceilings[b.name] <= tightest * 1.000001,
            )
        )

    # Buckets start full, so a run can deliver more than the steady-state rate
    # -- that surplus is the initial bucket, spent once. Utilisation above 100%
    # is exactly that signature, and is the honest way to detect it: a
    # wall-clock heuristic misses a long run whose head start still flatters the
    # average. Say so rather than letting the headline number imply the rate is
    # repeatable.
    burst_affected = any(r.utilisation > 1.0 for r in results)

    # The binding budget is the one that actually held requests back. A budget
    # can have the tightest ceiling and still never bind, because something
    # else -- worker concurrency, or simply not enough work -- got there first.
    binding = None
    if any(r.blocked for r in results):
        binding = max(results, key=lambda r: r.blocked).name

    return SimulationResult(
        requests=requests,
        workers=workers,
        keys=keys,
        wall_seconds=clock,
        # None, not infinity: nothing constrained the run, so there is no rate
        # to report. The budget utilisations still say how much was spent.
        achieved_rps=(completed / wall) if wall > 0 else None,
        throttled=throttled,
        total_wait_seconds=total_wait,
        budgets=results,
        binding=binding,
        concurrency_ceiling_rps=(workers / latency) if latency > 0 else None,
        burst_affected=burst_affected,
        completed=completed >= requests,
    )


def _format_window(window: timedelta) -> str:
    """Render a window the way the CLI accepts it."""
    seconds = int(window.total_seconds())
    for unit, size in (("d", 86400), ("h", 3600), ("m", 60)):
        if seconds % size == 0 and seconds >= size:
            return f"{seconds // size}{unit}"
    return f"{seconds}s"
