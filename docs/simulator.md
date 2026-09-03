# Simulator

```bash
smartratelimit simulate --rpm 500 --tpm 100000 \
    --requests 1000 --workers 20 --avg-tokens 2000
```

```
EXPECTED TRAFFIC
────────────────────────────────────────────────────
  Requests             1,000 over 20 workers
  Wall time            19.0 min
  Throughput           0.88 req/s   (53/min)

  BUDGET               CEILING        UTILISATION   HELD BACK
  requests                   500/min           11%           0
  tokens                      50/min          105%         950 <-- binding

  ! tokens is the binding budget.
    950 of 1,000 requests (95%) waited, 1.2s on average.
```

No requests are sent. The simulation runs the library's own token buckets on a
virtual clock, so an hour of traffic is modelled in milliseconds — and the
answer comes from the same code that paces your real requests.

## Why bother

Because the arithmetic is not obvious, and the intuitive answer is usually wrong.

The scenario above looks like a comfortable fit: 500 requests a minute allowed,
1,000 requests to send, 20 workers to send them. In fact **it takes nineteen
minutes**, and requests-per-minute never gets above 11%.

The token budget is the only one that matters. 100,000 tokens a minute at 2,000
tokens a request is **50 requests a minute** — a ceiling ten times tighter than
the request limit sitting next to it. Adding workers changes nothing:

```bash
smartratelimit simulate --tpm 100000 --avg-tokens 2000 --requests 500 --workers 2
smartratelimit simulate --tpm 100000 --avg-tokens 2000 --requests 500 --workers 50
```

Both finish at the same moment. Concurrency cannot buy tokens.

## Is it the limit, or is it me?

Pass `--latency` — your API's typical response time — and worker count becomes a
real ceiling that the simulator will compare against your budgets:

```bash
smartratelimit simulate --rpm 500 --requests 200 --workers 3 --latency 2
```

```
  BUDGET               CEILING        UTILISATION   HELD BACK
  requests                   500/min           18%           0
  concurrency                 90/min             —           —  <-- binding

  No request was ever held back: the limits are not your constraint.
    3 workers at 2.0s each caps you at 90/min, below every budget.
```

Worth knowing before you go asking a provider to raise a limit you are nowhere
near.

## More keys

Each key gets its own budgets, and work goes to whichever can serve it soonest:

```bash
smartratelimit simulate --rpm 500 --tpm 100000 \
    --requests 1000 --workers 20 --avg-tokens 2000 --keys 4
```

19 minutes becomes 4. Note it beats a straight quarter — four keys also means
four full buckets to open with.

## Budgets other than requests and tokens

`--rpm`, `--tpm` and `--rpd` are shorthand. Anything else is `--limit`, with
`--cost` for what one request spends of it:

```bash
smartratelimit simulate --limit images=50/1h --cost images=2 --requests 20
```

```
  BUDGET               CEILING        UTILISATION   HELD BACK
  images                        25/h           80%           0
```

Twenty requests at two images each is 80% of the hourly budget, spent instantly.
Nothing was throttled — but there is only 20% of the hour left.

## What it will not tell you

**It models your limiter, not the provider's.** It knows exactly when
smartratelimit will hold a request back, because that is arithmetic over budgets
you supplied.

It does **not** predict 429s. Those additionally depend on:

- burstiness at the provider's edge, and their undocumented burst allowances
- other clients sharing the same key
- the gap between the token count you *estimate* and the one you are *billed*
  for — you do not know the real number until the response arrives
- server-side changes to your limits that no header announced

A number like "expect 14.2% 429s" would be a guess wearing a decimal point. This
command deliberately does not produce one.

## Reading the output

**CEILING** is the sustained rate a budget permits: its refill rate divided by
what one request costs. This is the number to plan against.

**UTILISATION above 100%** is the opening burst. Buckets start full, so a run
spends one bucket's worth of head start that will not recur. The report says so
when it happens.

**HELD BACK** counts requests that particular budget made wait. A budget can
have the tightest ceiling and still hold nothing back, if something else — worker
concurrency, or simply not enough work — got there first. Only what actually
constrained the run is marked `<-- binding`.

## Scripting it

`--json` emits the same data for a machine:

```bash
smartratelimit simulate --rpm 500 --tpm 100000 --avg-tokens 2000 \
    --requests 1000 --workers 20 --json
```

```json
{
  "wall_seconds": 1140.0,
  "achieved_rps": 0.8772,
  "throttled": 950,
  "throttled_fraction": 0.95,
  "binding": "tokens",
  "budgets": [
    {"name": "requests", "ceiling_per_minute": 500.0, "utilisation": 0.1053, "blocked": 0},
    {"name": "tokens", "ceiling_per_minute": 50.0, "utilisation": 1.0526, "blocked": 950}
  ]
}
```

`achieved_rps` is `null` when the workload fits inside the opening burst: no time
passes, so there is no rate to report.

## From Python

```python
from datetime import timedelta
from smartratelimit.simulate import Budget, simulate

result = simulate(
    budgets=[
        Budget("requests", 500, timedelta(minutes=1), cost=1),
        Budget("tokens", 100_000, timedelta(minutes=1), cost=2_000),
    ],
    requests=1_000,
    workers=20,
)

print(result.binding)                       # 'tokens'
print(result.budget("tokens").ceiling_rps)  # 0.833 -> 50/min
```

Once the simulation tells you what binds, configure it for real — see
[multi-dimensional limits](concepts.md#6-dimensions):

```python
limiter.set_limit("api.openai.com", limit=500, window="1m")
limiter.set_limit("api.openai.com", limit=100_000, window="1m", dimension="tokens")
limiter.request("POST", url, json=payload, cost={"tokens": 2_000})
```
