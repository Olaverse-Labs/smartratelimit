"""Monitoring and metrics export for rate limits."""

import json
from datetime import datetime
from typing import Dict, List, Optional

from smartratelimit._time import utcnow
from smartratelimit.models import RateLimitStatus


class MetricsCollector:
    """Collects and exports rate limit metrics."""

    def __init__(self):
        """Initialize metrics collector."""
        self._metrics: Dict[str, Dict] = {}

    def record_request(
        self,
        endpoint: str,
        status_code: int,
        rate_limit_status: Optional[RateLimitStatus] = None,
    ) -> None:
        """
        Record a request and its rate limit status.

        Args:
            endpoint: API endpoint
            status_code: HTTP status code
            rate_limit_status: Current rate limit status
        """
        if endpoint not in self._metrics:
            self._metrics[endpoint] = {
                "total_requests": 0,
                "successful_requests": 0,
                "rate_limited_requests": 0,
                "other_errors": 0,
                "rate_limit_history": [],
            }

        metrics = self._metrics[endpoint]
        metrics["total_requests"] += 1

        if status_code == 429:
            metrics["rate_limited_requests"] += 1
        elif 200 <= status_code < 300:
            metrics["successful_requests"] += 1
        else:
            metrics["other_errors"] += 1

        if rate_limit_status:
            metrics["rate_limit_history"].append(
                {
                    "timestamp": utcnow().isoformat(),
                    "limit": rate_limit_status.limit,
                    "remaining": rate_limit_status.remaining,
                    "utilization": rate_limit_status.utilization,
                    "reset_in": rate_limit_status.reset_in,
                }
            )
            # Keep only last 100 entries
            if len(metrics["rate_limit_history"]) > 100:
                metrics["rate_limit_history"] = metrics["rate_limit_history"][-100:]

    def get_metrics(self, endpoint: Optional[str] = None) -> Dict:
        """
        Get metrics for endpoint(s).

        Args:
            endpoint: Specific endpoint, or None for all

        Returns:
            Metrics dictionary
        """
        if endpoint:
            return self._metrics.get(endpoint, {})
        return self._metrics

    def export_prometheus(self) -> str:
        """
        Export metrics in Prometheus format.

        Returns:
            Prometheus metrics string
        """
        lines = []
        lines.append("# HELP ratelimit_total_requests Total number of requests")
        lines.append("# TYPE ratelimit_total_requests counter")
        lines.append("# HELP ratelimit_successful_requests Number of successful requests")
        lines.append("# TYPE ratelimit_successful_requests counter")
        lines.append("# HELP ratelimit_rate_limited_requests Number of rate-limited requests")
        lines.append("# TYPE ratelimit_rate_limited_requests counter")
        lines.append("# HELP ratelimit_other_errors Number of other errors")
        lines.append("# TYPE ratelimit_other_errors counter")
        lines.append("# HELP ratelimit_remaining Remaining requests in current window")
        lines.append("# TYPE ratelimit_remaining gauge")
        lines.append("# HELP ratelimit_limit Total limit for current window")
        lines.append("# TYPE ratelimit_limit gauge")
        lines.append("# HELP ratelimit_utilization Current utilization (0.0-1.0)")
        lines.append("# TYPE ratelimit_utilization gauge")

        for endpoint, metrics in self._metrics.items():
            endpoint_label = endpoint.replace("://", "_").replace("/", "_").replace(".", "_")
            lines.append(
                f'ratelimit_total_requests{{endpoint="{endpoint}"}} {metrics["total_requests"]}'
            )
            lines.append(
                f'ratelimit_successful_requests{{endpoint="{endpoint}"}} {metrics["successful_requests"]}'
            )
            lines.append(
                f'ratelimit_rate_limited_requests{{endpoint="{endpoint}"}} {metrics["rate_limited_requests"]}'
            )
            lines.append(
                f'ratelimit_other_errors{{endpoint="{endpoint}"}} {metrics["other_errors"]}'
            )

            # Get latest rate limit status
            if metrics["rate_limit_history"]:
                latest = metrics["rate_limit_history"][-1]
                lines.append(
                    f'ratelimit_remaining{{endpoint="{endpoint}"}} {latest["remaining"]}'
                )
                lines.append(
                    f'ratelimit_limit{{endpoint="{endpoint}"}} {latest["limit"]}'
                )
                lines.append(
                    f'ratelimit_utilization{{endpoint="{endpoint}"}} {latest["utilization"]}'
                )

        return "\n".join(lines) + "\n"

    def export_json(self) -> str:
        """
        Export metrics as JSON.

        Returns:
            JSON string
        """
        return json.dumps(self._metrics, indent=2)

    def reset(self, endpoint: Optional[str] = None) -> None:
        """
        Reset metrics for endpoint(s).

        Args:
            endpoint: Specific endpoint, or None for all
        """
        if endpoint:
            self._metrics.pop(endpoint, None)
        else:
            self._metrics.clear()

