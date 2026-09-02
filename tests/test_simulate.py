"""Tests for the workload simulator."""

import json
import subprocess
import sys
from datetime import timedelta

import pytest

from smartratelimit.simulate import Budget, SimulationError, simulate

MINUTE = timedelta(minutes=1)
HOUR = timedelta(hours=1)


def rpm(limit, cost=1.0):
    return Budget("requests", limit, MINUTE, cost=cost)


def tpm(limit, cost):
    return Budget("tokens", limit, MINUTE, cost=cost)


class TestCeilings:
    """The arithmetic the whole thing rests on."""

    def test_ceiling_is_refill_rate_over_cost(self):
        # 100k tokens a minute at 2k a request is 50 requests a minute.
        assert tpm(100_000, 2000).ceiling_rps * 60 == pytest.approx(50)

    def test_request_budget_ceiling(self):
        assert rpm(500).ceiling_rps * 60 == pytest.approx(500)

    def test_free_dimension_has_no_ceiling(self):
        assert Budget("free", 10, MINUTE, cost=0).ceiling_rps == float("inf")


class TestBindingBudget:
    """Which budget actually holds the workload up."""

    def test_tokens_bind_before_requests(self):
        """The case the whole feature exists for: TPM binds, RPM idles."""
        result = simulate([rpm(500), tpm(100_000, 2000)], requests=1000, workers=20)

        assert result.binding == "tokens"
        assert result.budget("tokens").blocked > 0
        assert result.budget("requests").blocked == 0
        # RPM is nowhere near its ceiling while tokens are pinned.
        assert result.budget("requests").utilisation < 0.2
        assert result.budget("tokens").utilisation > 0.95

    def test_requests_bind_when_tokens_are_plentiful(self):
        result = simulate([rpm(60), tpm(10_000_000, 10)], requests=200, workers=5)

        assert result.binding == "requests"
        assert result.budget("tokens").blocked == 0

    def test_concurrency_is_irrelevant_when_a_budget_binds(self):
        """More workers cannot beat a token budget."""
        few = simulate([tpm(100_000, 2000)], requests=500, workers=2)
        many = simulate([tpm(100_000, 2000)], requests=500, workers=50)

        assert few.wall_seconds == pytest.approx(many.wall_seconds, rel=0.01)

    def test_nothing_binds_when_the_budget_is_ample(self):
        result = simulate([rpm(10_000)], requests=50, workers=1)

        assert result.binding is None
        assert result.throttled == 0


class TestKeys:
    """Spreading across API keys multiplies the ceiling."""

    def test_four_keys_quadruple_the_ceiling(self):
        one = simulate([tpm(100_000, 2000)], requests=1000, workers=20, keys=1)
        four = simulate([tpm(100_000, 2000)], requests=1000, workers=20, keys=4)

        # The sustained ceiling scales exactly.
        assert four.budget("tokens").ceiling_rps == pytest.approx(
            one.budget("tokens").ceiling_rps * 4
        )
        # Wall time beats a straight quarter, because each key also brings its
        # own full bucket to start with — four opening bursts, not one.
        assert four.wall_seconds < one.wall_seconds / 4
        assert four.wall_seconds > one.wall_seconds / 8

    def test_each_key_has_its_own_budget(self):
        """Two keys means two full buckets, so twice the opening burst."""
        result = simulate([rpm(10)], requests=20, workers=1, keys=2)

        assert result.throttled == 0


class TestConcurrency:
    """Latency turns worker count into a real ceiling."""

    def test_workers_can_be_the_bottleneck(self):
        result = simulate([rpm(500)], requests=200, workers=3, latency=2.0)

        # 3 workers at 2s each is 1.5/s, well under the 500/min budget.
        assert result.concurrency_ceiling_rps == pytest.approx(1.5)
        assert result.achieved_rps == pytest.approx(1.5, rel=0.05)
        assert result.throttled == 0
        assert result.binding is None

    def test_no_concurrency_ceiling_without_latency(self):
        assert simulate([rpm(500)], requests=10).concurrency_ceiling_rps is None


class TestInstantaneousRun:
    """A workload that fits in the opening burst finishes in zero time."""

    def test_throughput_is_undefined_not_infinite(self):
        """Dividing by a zero-length run produced 2e10 req/s before this."""
        result = simulate([Budget("images", 50, HOUR, cost=2)], requests=20)

        assert result.wall_seconds == 0
        assert result.achieved_rps is None

    def test_utilisation_is_measured_over_a_full_window(self):
        """40 of a 50-per-hour budget is 80% of that hour, not 2.88e12%."""
        result = simulate([Budget("images", 50, HOUR, cost=2)], requests=20)

        assert result.budget("images").utilisation == pytest.approx(0.8)

    def test_burst_is_not_flagged_when_nothing_is_overstated(self):
        """80% of an hour's budget spent instantly overstates no rate."""
        result = simulate([Budget("images", 50, HOUR, cost=2)], requests=20)

        assert result.burst_affected is False

    def test_burst_is_flagged_when_it_inflates_the_rate(self):
        """A run that delivers >100% of a budget did so on its head start."""
        result = simulate([rpm(500), tpm(100_000, 2000)], requests=1000, workers=20)

        assert result.budget("tokens").utilisation > 1.0
        assert result.burst_affected is True


class TestValidation:
    """Refuse to model what cannot happen."""

    def test_request_costing_more_than_the_whole_budget(self):
        with pytest.raises(SimulationError, match="no request can ever be sent"):
            simulate([tpm(1000, 5000)], requests=1)

    @pytest.mark.parametrize(
        "kwargs", [{"requests": 0}, {"requests": 5, "workers": 0}, {"requests": 5, "keys": 0}]
    )
    def test_non_positive_inputs(self, kwargs):
        with pytest.raises(SimulationError):
            simulate([rpm(10)], **kwargs)

    def test_no_budgets(self):
        with pytest.raises(SimulationError, match="at least one budget"):
            simulate([], requests=10)

    def test_non_positive_limit(self):
        with pytest.raises(SimulationError, match="non-positive"):
            simulate([rpm(0)], requests=10)


class TestDeterminism:
    """A simulator that wobbles is not worth running."""

    def test_same_inputs_give_the_same_answer(self):
        runs = [
            simulate([rpm(500), tpm(100_000, 2000)], requests=300, workers=10)
            for _ in range(3)
        ]
        assert len({r.wall_seconds for r in runs}) == 1
        assert len({r.throttled for r in runs}) == 1


def run_cli(*args):
    return subprocess.run(
        [sys.executable, "-m", "smartratelimit.cli", "simulate", *args],
        capture_output=True,
        text=True,
    )


class TestCLI:
    """The command as a user meets it."""

    def test_reports_the_binding_budget(self):
        result = run_cli(
            "--rpm", "500", "--tpm", "100000",
            "--requests", "1000", "--workers", "20", "--avg-tokens", "2000",
        )

        assert result.returncode == 0
        assert "tokens is the binding budget" in result.stdout
        assert "<-- binding" in result.stdout

    def test_states_what_it_cannot_know(self):
        """The honesty line is load-bearing, not decoration."""
        result = run_cli("--rpm", "60", "--requests", "10")

        assert "not the provider's" in result.stdout
        assert "429" in result.stdout

    def test_json_output_is_machine_readable(self):
        result = run_cli(
            "--rpm", "500", "--tpm", "100000",
            "--requests", "100", "--avg-tokens", "2000", "--json",
        )

        data = json.loads(result.stdout)
        assert data["binding"] == "tokens"
        assert data["requests"] == 100
        assert {b["name"] for b in data["budgets"]} == {"requests", "tokens"}

    def test_json_survives_an_instantaneous_run(self):
        """achieved_rps is null here; it must not blow up the encoder."""
        result = run_cli("--limit", "images=50/1h", "--cost", "images=2",
                         "--requests", "20", "--json")

        data = json.loads(result.stdout)
        assert data["achieved_rps"] is None
        assert data["budgets"][0]["utilisation"] == pytest.approx(0.8)

    def test_custom_budget(self):
        result = run_cli("--limit", "images=50/1h", "--cost", "images=2",
                         "--requests", "20")

        assert result.returncode == 0
        # 50/hour must not render as "0/min".
        assert "0/min" not in result.stdout
        assert "/h" in result.stdout

    def test_requires_a_limit(self):
        result = run_cli("--requests", "10")

        assert result.returncode == 1
        assert "at least one limit" in result.stderr

    def test_rejects_impossible_scenario(self):
        result = run_cli("--tpm", "1000", "--avg-tokens", "5000", "--requests", "5")

        assert result.returncode == 1
        assert "no request can ever be sent" in result.stderr

    def test_rejects_malformed_limit(self):
        result = run_cli("--limit", "nonsense")

        assert result.returncode != 0
        assert "NAME=COUNT/WINDOW" in result.stderr

    def test_rejects_bad_window(self):
        result = run_cli("--limit", "x=10/1.5h")

        assert result.returncode != 0
        assert "Invalid window" in result.stderr

    def test_the_engine_cannot_reach_the_network(self):
        """It is a simulator: no import path from it should reach a socket."""
        import smartratelimit.simulate as sim

        source = open(sim.__file__).read()
        for forbidden in ("import requests", "import socket", "import urllib", "urlopen"):
            assert forbidden not in source, f"simulate.py should not {forbidden}"
