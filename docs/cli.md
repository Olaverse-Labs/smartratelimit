# CLI

Installing the package puts a `smartratelimit` command on your PATH. It exists mainly to answer two questions from a shell: *what does this API advertise?* and *what quota does my app think it has left?*

```bash
smartratelimit --help
python -m smartratelimit.cli --help     # equivalent, if the script isn't on PATH
```

!!! warning "`--storage` comes before the subcommand"
    It is a global option, and it defaults to `memory` — which is empty in a
    fresh process. `status` and `clear` are only meaningful against the same
    **SQLite or Redis** backend your application writes to.

    ```bash
    smartratelimit --storage "sqlite:///ratelimit.db" status "https://api.github.com"
    ```

## `probe`

Send one request and show what came back, headers and all. This is the command you run before writing any configuration.

```bash
smartratelimit probe "https://api.github.com/users/octocat"
```

```
Probing https://api.github.com/users/octocat...

Response Status: 200

Rate Limit Headers:
  X-RateLimit-Limit: 60
  X-RateLimit-Remaining: 59
  X-RateLimit-Reset: 1734567890

Detected Rate Limit:
  Limit: 60
  Remaining: 59
  Window: 0:59:31
```

Read it in two parts:

- **Rate Limit Headers** — what the API actually sent, of the names the CLI prints
- **Detected Rate Limit** — what the library made of it

If the first section shows limit-ish headers and the second says nothing was detected, the API uses names the detector doesn't know: give it a [`headers_map`](detection.md#2-custom-header-map).

Probing writes to the chosen backend, so you can seed a shared database and inspect it afterwards:

```bash
smartratelimit --storage "sqlite:///ratelimit.db" probe "https://api.agify.io?name=Michael"
```

`probe` issues a real GET. Don't point it at anything with side effects.

## `status`

Read the stored quota for one endpoint.

```bash
smartratelimit --storage "sqlite:///ratelimit.db" status "https://api.github.com"
```

```
Endpoint: https://api.github.com
  Limit: 5000
  Remaining: 4958
  Utilization: 0.8%
  Resets at: 2026-08-15 11:18:25.481203
  Resets in: 2842 seconds
  Exceeded: False
```

Nothing stored yet:

```
Endpoint: https://api.github.com
  No rate limit information available
```

The endpoint argument is positional and required — a bare domain works as well as a full URL, since both normalise to `scheme://host`. Times are UTC.

## `clear`

Forget a stored quota, so the next request re-learns it from response headers.

```bash
smartratelimit --storage "sqlite:///ratelimit.db" clear "https://api.github.com"   # one endpoint
smartratelimit --storage "sqlite:///ratelimit.db" clear                            # everything
```

With no argument it clears the whole backend — including endpoints belonging to other applications sharing that database. Name the endpoint unless you mean all of it.

## `list`

Currently a placeholder: enumerating stored endpoints isn't part of the storage interface yet, so the command prints a note pointing you at `status`.

```bash
smartratelimit list
```

## A working loop

The CLI is only useful when it reads the same state your code writes, so give both a real backend:

```python
# app.py
from smartratelimit import RateLimiter

limiter = RateLimiter(storage="sqlite:///ratelimit.db")

for name in ["Michael", "Sarah", "Alex"]:
    r = limiter.request("GET", "https://api.agify.io", params={"name": name})
    print(name, r.json()["age"])
```

```bash
python app.py
smartratelimit --storage "sqlite:///ratelimit.db" status "https://api.agify.io"
```

## Watching quota from cron

Because state is in the backend rather than the process, a monitoring job can check quota without touching the application:

```bash
*/5 * * * * smartratelimit --storage "redis://localhost:6379/0" \
    status "https://api.github.com" >> /var/log/ratelimit.log 2>&1
```

For anything richer than a log line, read the same backend from Python and export [metrics](metrics.md) — the CLI prints for humans, not for scrapers.
