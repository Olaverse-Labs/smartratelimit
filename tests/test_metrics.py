"""Tests for metrics collection."""

from datetime import datetime, timedelta

import pytest

# The library stores naive-UTC timestamps; using its own clock helpers
# keeps these fixtures on exactly the same footing as the code under test.
from smartratelimit._time import utcnow
from smartratelimit.metrics import MetricsCollector
from smartratelimit.models import RateLimitStatus


class TestMetricsCollector:
    """Test MetricsCollector."""

    def test_init(self):
        """Test metrics collector initialization."""
        collector = MetricsCollector()
        assert collector._metrics == {}

    def test_record_request_success(self):
        """Test recording successful request."""
        collector = MetricsCollector()
        status = RateLimitStatus(
            endpoint="https://api.example.com",
            limit=100,
            remaining=99,
            reset_time=utcnow() + timedelta(hours=1),
        )

        collector.record_request("https://api.example.com", 200, status)

        metrics = collector.get_metrics("https://api.example.com")
        assert metrics["total_requests"] == 1
        assert metrics["successful_requests"] == 1
        assert metrics["rate_limited_requests"] == 0
        assert metrics["other_errors"] == 0
        assert len(metrics["rate_limit_history"]) == 1

    def test_record_request_rate_limited(self):
        """Test recording rate-limited request."""
        collector = MetricsCollector()
        status = RateLimitStatus(
            endpoint="https://api.example.com",
            limit=100,
            remaining=0,
            reset_time=utcnow() + timedelta(hours=1),
        )

        collector.record_request("https://api.example.com", 429, status)

        metrics = collector.get_metrics("https://api.example.com")
        assert metrics["total_requests"] == 1
        assert metrics["successful_requests"] == 0
        assert metrics["rate_limited_requests"] == 1
        assert metrics["other_errors"] == 0

    def test_record_request_error(self):
        """Test recording error request."""
        collector = MetricsCollector()
        collector.record_request("https://api.example.com", 500, None)

        metrics = collector.get_metrics("https://api.example.com")
        assert metrics["total_requests"] == 1
        assert metrics["successful_requests"] == 0
        assert metrics["rate_limited_requests"] == 0
        assert metrics["other_errors"] == 1

    def test_record_multiple_requests(self):
        """Test recording multiple requests."""
        collector = MetricsCollector()
        status = RateLimitStatus(
            endpoint="https://api.example.com",
            limit=100,
            remaining=98,
            reset_time=utcnow() + timedelta(hours=1),
        )

        collector.record_request("https://api.example.com", 200, status)
        collector.record_request("https://api.example.com", 200, status)
        collector.record_request("https://api.example.com", 429, status)

        metrics = collector.get_metrics("https://api.example.com")
        assert metrics["total_requests"] == 3
        assert metrics["successful_requests"] == 2
        assert metrics["rate_limited_requests"] == 1

    def test_get_metrics_specific_endpoint(self):
        """Test getting metrics for specific endpoint."""
        collector = MetricsCollector()
        collector.record_request("https://api1.com", 200, None)
        collector.record_request("https://api2.com", 200, None)

        metrics1 = collector.get_metrics("https://api1.com")
        metrics2 = collector.get_metrics("https://api2.com")

        assert metrics1["total_requests"] == 1
        assert metrics2["total_requests"] == 1

    def test_get_metrics_all(self):
        """Test getting all metrics."""
        collector = MetricsCollector()
        collector.record_request("https://api1.com", 200, None)
        collector.record_request("https://api2.com", 200, None)

        all_metrics = collector.get_metrics()
        assert len(all_metrics) == 2
        assert "https://api1.com" in all_metrics
        assert "https://api2.com" in all_metrics

    def test_get_metrics_nonexistent(self):
        """Test getting metrics for nonexistent endpoint."""
        collector = MetricsCollector()
        metrics = collector.get_metrics("https://api.example.com")
        assert metrics == {}

    def test_export_prometheus(self):
        """Test Prometheus export."""
        collector = MetricsCollector()
        status = RateLimitStatus(
            endpoint="https://api.example.com",
            limit=100,
            remaining=99,
            reset_time=utcnow() + timedelta(hours=1),
        )

        collector.record_request("https://api.example.com", 200, status)
        prometheus = collector.export_prometheus()

        assert "ratelimit_total_requests" in prometheus
        assert "ratelimit_successful_requests" in prometheus
        assert "ratelimit_remaining" in prometheus
        assert "api.example.com" in prometheus

    def test_export_json(self):
        """Test JSON export."""
        collector = MetricsCollector()
        collector.record_request("https://api.example.com", 200, None)

        json_metrics = collector.export_json()
        assert isinstance(json_metrics, str)
        assert "api.example.com" in json_metrics
        assert "total_requests" in json_metrics

    def test_reset_specific_endpoint(self):
        """Test resetting metrics for specific endpoint."""
        collector = MetricsCollector()
        collector.record_request("https://api1.com", 200, None)
        collector.record_request("https://api2.com", 200, None)

        collector.reset("https://api1.com")

        assert collector.get_metrics("https://api1.com") == {}
        assert collector.get_metrics("https://api2.com")["total_requests"] == 1

    def test_reset_all(self):
        """Test resetting all metrics."""
        collector = MetricsCollector()
        collector.record_request("https://api1.com", 200, None)
        collector.record_request("https://api2.com", 200, None)

        collector.reset()

        assert collector.get_metrics() == {}

    def test_rate_limit_history_limit(self):
        """Test rate limit history is limited to 100 entries."""
        collector = MetricsCollector()
        status = RateLimitStatus(
            endpoint="https://api.example.com",
            limit=100,
            remaining=99,
            reset_time=utcnow() + timedelta(hours=1),
        )

        # Record more than 100 requests
        for _ in range(150):
            collector.record_request("https://api.example.com", 200, status)

        metrics = collector.get_metrics("https://api.example.com")
        assert len(metrics["rate_limit_history"]) == 100

