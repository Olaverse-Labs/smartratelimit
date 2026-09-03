# Playground

The simulator below is not a mock of smartratelimit. It runs the library's own
simulation modules — copied verbatim from the source this site was built from —
inside a Python runtime in your browser, driving the same `TokenBucket` that
paces your production requests.

Nothing is sent anywhere. Everything runs on your machine.

??? note "Why it loads source files rather than installing the package"
    Two reasons, both worth knowing if you ever try this yourself.

    `smartratelimit/__init__.py` eagerly imports the HTTP and storage stack, and
    `storage.py` imports `sqlite3` — which Pyodide unvendors from the standard
    library. Importing the installed package in a browser fails outright.

    And PyPI serves the last *release*, which is not necessarily the code these
    docs describe. A page demonstrating the docs should run the source the docs
    were built from.

    So the three modules the simulator needs — `_time`, `models`, `simulate` —
    are published alongside this page and written straight into Pyodide's
    filesystem. They import nothing but the standard library, so nothing has to
    resolve. They are byte-identical to the package, and a test fails if they
    ever drift. Only the package `__init__` is stubbed, because that is the file
    that drags in sqlite3.

<div id="srl-playground" class="srl">
  <div class="srl-status" id="srl-status">
    <span class="srl-spinner"></span>
    <span id="srl-status-text">Starting Python in your browser…</span>
  </div>

  <div class="srl-panel" id="srl-controls" hidden>
    <div class="srl-controls">
      <label>Requests per minute
        <input type="range" id="rpm" min="10" max="5000" step="10" value="500">
        <output for="rpm" id="rpm-out">500</output>
      </label>
      <label>Tokens per minute
        <input type="range" id="tpm" min="10000" max="1000000" step="10000" value="100000">
        <output for="tpm" id="tpm-out">100,000</output>
      </label>
      <label>Average tokens per request
        <input type="range" id="avg" min="100" max="20000" step="100" value="2000">
        <output for="avg" id="avg-out">2,000</output>
      </label>
      <label>Requests to send
        <input type="range" id="reqs" min="10" max="5000" step="10" value="1000">
        <output for="reqs" id="reqs-out">1,000</output>
      </label>
      <label>Concurrent workers
        <input type="range" id="workers" min="1" max="100" step="1" value="20">
        <output for="workers" id="workers-out">20</output>
      </label>
      <label>API keys
        <input type="range" id="keys" min="1" max="10" step="1" value="1">
        <output for="keys" id="keys-out">1</output>
      </label>
      <label>Response latency (seconds)
        <input type="range" id="latency" min="0" max="10" step="0.5" value="0">
        <output for="latency" id="latency-out">0 — limits only</output>
      </label>
    </div>
    <pre class="srl-out" id="srl-out">—</pre>
  </div>
</div>

<style>
.srl { margin: 1.2rem 0; }
.srl-status {
  display: flex; align-items: center; gap: .6rem;
  padding: .8rem 1rem; border-radius: .3rem;
  background: var(--md-code-bg-color); font-size: .8rem;
}
.srl-status.srl-error { border-left: .2rem solid #d32f2f; }
.srl-spinner {
  width: .9rem; height: .9rem; flex: none; border-radius: 50%;
  border: 2px solid var(--md-default-fg-color--lightest);
  border-top-color: var(--md-primary-fg-color);
  animation: srl-spin .8s linear infinite;
}
.srl-status.srl-done .srl-spinner,
.srl-status.srl-error .srl-spinner { display: none; }
@keyframes srl-spin { to { transform: rotate(360deg); } }
.srl-controls {
  display: grid; grid-template-columns: repeat(auto-fit, minmax(15rem, 1fr));
  gap: .7rem 1.4rem; margin-bottom: 1rem;
}
.srl-controls label {
  display: grid; grid-template-columns: 1fr auto; gap: .1rem .6rem;
  font-size: .72rem; align-items: center;
}
.srl-controls input[type=range] { grid-column: 1; width: 100%; }
.srl-controls output {
  grid-column: 2; grid-row: 2; font-family: var(--md-code-font-family);
  font-size: .7rem; color: var(--md-default-fg-color--light); white-space: nowrap;
}
.srl-out {
  font-family: var(--md-code-font-family); font-size: .72rem; line-height: 1.5;
  white-space: pre; overflow-x: auto; padding: 1rem;
  background: var(--md-code-bg-color); border-radius: .3rem; margin: 0;
}
.srl-bind { color: #d32f2f; font-weight: 700; }
</style>

<script src="https://cdn.jsdelivr.net/pyodide/v0.26.4/full/pyodide.js"></script>
<script>
(function () {
  var statusEl = document.getElementById("srl-status");
  var statusText = document.getElementById("srl-status-text");
  var controls = document.getElementById("srl-controls");
  var out = document.getElementById("srl-out");
  var pyodide = null;

  var IDS = ["rpm", "tpm", "avg", "reqs", "workers", "keys", "latency"];

  function say(text, kind) {
    statusText.textContent = text;
    statusEl.className = "srl-status" + (kind ? " srl-" + kind : "");
  }

  function fmt(n) { return Number(n).toLocaleString(); }

  function syncLabels() {
    document.getElementById("rpm-out").textContent = fmt(rpm.value) + "/min";
    document.getElementById("tpm-out").textContent = fmt(tpm.value) + "/min";
    document.getElementById("avg-out").textContent = fmt(avg.value);
    document.getElementById("reqs-out").textContent = fmt(reqs.value);
    document.getElementById("workers-out").textContent = workers.value;
    document.getElementById("keys-out").textContent = keys.value;
    document.getElementById("latency-out").textContent =
      latency.value === "0" ? "0 — limits only" : latency.value + "s";
  }

  // Rendering lives in JS so the Python side stays exactly the library's own
  // API: build Budgets, call simulate(), hand back the result. Nothing about
  // the model is reimplemented here.
  function render(r) {
    var lines = [];
    var workersLabel = r.workers + (r.workers === 1 ? " worker" : " workers");
    var keysLabel = r.keys === 1 ? "" : " and " + r.keys + " keys";

    lines.push("EXPECTED TRAFFIC");
    lines.push("─".repeat(56));
    lines.push("  Requests             " + fmt(r.requests) + " over " + workersLabel + keysLabel);

    if (r.achieved_rps === null) {
      lines.push("  Wall time            instant — the whole workload fit in one burst");
    } else {
      var mins = r.wall_seconds / 60;
      var dur = r.wall_seconds < 90
        ? r.wall_seconds.toFixed(1) + "s"
        : (mins < 90 ? mins.toFixed(1) + " min" : (mins / 60).toFixed(1) + " h");
      lines.push("  Wall time            " + dur);
      lines.push("  Throughput           " + r.achieved_rps.toFixed(2) + " req/s");
    }

    lines.push("");
    // Header built with the same padding as the rows: a hand-counted string
    // drifts the moment a column width changes, and it already had.
    function row(name, ceiling, util, blocked) {
      return "  " + name.padEnd(20) + ceiling.padStart(14) +
             util.padStart(13) + blocked.padStart(12);
    }
    lines.push(row("BUDGET", "CEILING", "UTILISATION", "HELD BACK"));
    r.budgets.forEach(function (b) {
      var perMin = b.ceiling_per_minute;
      var ceiling = perMin >= 1 ? fmt(Math.round(perMin)) + "/min"
                                : fmt(Math.round(perMin * 60)) + "/h";
      var mark = b.name === r.binding ? "  <-- binding" : "";
      lines.push(row(b.name, ceiling,
                     Math.round(b.utilisation * 100) + "%",
                     fmt(b.blocked)) + mark);
    });
    if (r.concurrency_ceiling_rps !== null) {
      var cc = fmt(Math.round(r.concurrency_ceiling_rps * 60)) + "/min";
      var ccBinds = r.binding === null &&
        r.concurrency_ceiling_rps < Math.min.apply(null,
          r.budgets.map(function (b) { return b.ceiling_per_minute / 60; }));
      lines.push(row("concurrency", cc, "—", "—") + (ccBinds ? "  <-- binding" : ""));
    }

    lines.push("");
    if (!r.completed) {
      lines.push("  ! Did not finish within the simulated horizon — these limits are");
      lines.push("    far below this workload.");
    } else if (r.binding === null) {
      lines.push("  No request was ever held back: the limits are not your constraint.");
    } else {
      lines.push("  ! " + r.binding + " is the binding budget.");
      lines.push("    " + fmt(r.throttled) + " of " + fmt(r.requests) + " requests (" +
                 Math.round(r.throttled_fraction * 100) + "%) waited.");
    }
    if (r.burst_affected) {
      lines.push("");
      lines.push("  Buckets start full, so this run spent a head start that will not");
      lines.push("  recur. CEILING is the rate you can actually sustain.");
    }

    out.textContent = lines.join("\n");
  }

  function run() {
    if (!pyodide) return;
    try {
      var result = pyodide.runPython(
        "run_simulation(" + [
          rpm.value, tpm.value, avg.value, reqs.value, workers.value, keys.value, latency.value
        ].join(", ") + ")"
      );
      render(JSON.parse(result));
    } catch (err) {
      out.textContent = String(err.message || err).split("\n").slice(-6).join("\n");
    }
  }

  // The three modules the simulator needs, copied into the site by
  // hooks/playground_modules.py. They import nothing but the standard library,
  // so there is no package to install and no dependency to resolve — which is
  // what makes this work at all: smartratelimit's real __init__ pulls in
  // sqlite3, and Pyodide unvendors sqlite3 from the stdlib.
  var MODULES = ["__init__.py", "_time.py", "models.py", "simulate.py"];

  function assetsRoot() {
    // Derive it from a stylesheet the theme always emits, so this works whether
    // or not the site uses directory URLs.
    var link = document.querySelector('link[href*="/assets/stylesheets/"]');
    if (link) return link.href.replace(/\/assets\/stylesheets\/.*$/, "/assets/");
    return new URL("../assets/", window.location.href).href;
  }

  async function boot() {
    if (typeof loadPyodide !== "function") {
      say("Could not load Pyodide. A network policy or content blocker may be " +
          "stopping cdn.jsdelivr.net. The CLI does the same thing offline: " +
          "smartratelimit simulate --rpm 500 --tpm 100000", "error");
      return;
    }
    try {
      say("Starting Python in your browser… (~10 MB, first load only)");
      pyodide = await loadPyodide();

      say("Loading smartratelimit…");
      var base = assetsRoot() + "py/smartratelimit/";
      var sources = await Promise.all(MODULES.map(async function (name) {
        var response = await fetch(base + name);
        if (!response.ok) {
          throw new Error("could not fetch " + name + " (" + response.status + ")");
        }
        return response.text();
      }));

      pyodide.FS.mkdirTree("/srl/smartratelimit");
      MODULES.forEach(function (name, i) {
        pyodide.FS.writeFile("/srl/smartratelimit/" + name, sources[i]);
      });
      pyodide.runPython("import sys\nsys.path.insert(0, '/srl')");

      pyodide.runPython([
        "import json",
        "from datetime import timedelta",
        "from smartratelimit.simulate import Budget, SimulationError, simulate",
        "",
        "MINUTE = timedelta(minutes=1)",
        "",
        "def run_simulation(rpm, tpm, avg_tokens, requests, workers, keys, latency):",
        "    budgets = [",
        "        Budget('requests', int(rpm), MINUTE, cost=1.0),",
        "        Budget('tokens', int(tpm), MINUTE, cost=float(avg_tokens)),",
        "    ]",
        "    try:",
        "        r = simulate(budgets, requests=int(requests), workers=int(workers),",
        "                     keys=int(keys), latency=float(latency))",
        "    except SimulationError as e:",
        "        raise ValueError(str(e))",
        "    return json.dumps({",
        "        'requests': r.requests, 'workers': r.workers, 'keys': r.keys,",
        "        'completed': r.completed, 'wall_seconds': r.wall_seconds,",
        "        'achieved_rps': r.achieved_rps, 'throttled': r.throttled,",
        "        'throttled_fraction': r.throttled_fraction, 'binding': r.binding,",
        "        'concurrency_ceiling_rps': r.concurrency_ceiling_rps,",
        "        'burst_affected': r.burst_affected,",
        "        'budgets': [{'name': b.name, 'ceiling_per_minute': b.ceiling_rps * 60,",
        "                     'utilisation': b.utilisation, 'blocked': b.blocked}",
        "                    for b in r.budgets],",
        "    })",
      ].join("\n"));

      say("Running smartratelimit in your browser. Move a slider.", "done");
      controls.hidden = false;
      syncLabels();
      run();
    } catch (err) {
      say("Could not start the demo: " + (err.message || err) +
          " — the CLI does the same thing offline.", "error");
    }
  }

  IDS.forEach(function (id) {
    var el = document.getElementById(id);
    window[id] = el;
    el.addEventListener("input", function () { syncLabels(); run(); });
  });

  syncLabels();
  boot();
})();
</script>

## Things worth trying

**Drag *Average tokens per request* up.** Watch the tokens ceiling collapse while
the request ceiling sits untouched. At 2,000 tokens against 100,000 a minute you
get 50 requests a minute — a tenth of a 500 RPM limit.

**Then drag *Concurrent workers*.** Nothing happens. Concurrency cannot buy
tokens, and no amount of parallelism moves a budget-bound workload.

**Now raise *API keys*.** That does move it, because each key carries its own
budgets — and it beats a straight multiple, since each also brings a full bucket
to open with.

**Set *Response latency* to something realistic.** A concurrency row appears. If
it comes out below every budget, your worker count is the constraint and raising
a rate limit would change nothing.

## The point

Every number here comes from `smartratelimit.simulate`, which drives the same
`TokenBucket` the limiter uses against live traffic. If this page and the library
ever disagreed, one of them would be wrong — and there is only one implementation
for them to disagree about. The JavaScript here formats a result; it never
computes one.

For scripting, scenarios beyond requests and tokens, and what this deliberately
will not tell you about 429s, see [Simulator](simulator.md).
