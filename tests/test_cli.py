"""Tests for CLI tools."""

import sys
from datetime import datetime, timedelta
from io import StringIO
from unittest.mock import patch

import pytest

# The library stores naive-UTC timestamps; using its own clock helpers
# keeps these fixtures on exactly the same footing as the code under test.
from smartratelimit._time import utcnow
from smartratelimit.cli import cmd_clear, cmd_probe, cmd_status, main
from smartratelimit.models import RateLimitStatus


class TestCLI:
    """Test CLI functionality."""

    def test_cmd_status(self):
        """Test status command."""
        with patch("sys.argv", ["smartratelimit", "status", "api.example.com"]):
            with patch("smartratelimit.cli.RateLimiter") as mock_limiter_class:
                mock_limiter = mock_limiter_class.return_value
                # The real dataclass, so this stub cannot drift out of sync
                # with the fields the CLI prints.
                mock_limiter.get_status.return_value = RateLimitStatus(
                    endpoint="https://api.example.com",
                    limit=100,
                    remaining=50,
                    reset_time=utcnow() + timedelta(hours=1),
                    window=timedelta(hours=1),
                )

                # Capture stdout
                old_stdout = sys.stdout
                sys.stdout = StringIO()

                try:
                    cmd_status(type("Args", (), {"endpoint": "api.example.com", "storage": "memory"})())
                    output = sys.stdout.getvalue()
                    assert "Limit: 100" in output
                    assert "Remaining: 50" in output
                    assert "Confidence: confirmed" in output
                finally:
                    sys.stdout = old_stdout

    def test_cmd_status_no_endpoint(self):
        """Test status command without endpoint."""
        old_stderr = sys.stderr
        sys.stderr = StringIO()

        try:
            with pytest.raises(SystemExit):
                cmd_status(type("Args", (), {"endpoint": None, "storage": "memory"})())
            output = sys.stderr.getvalue()
            assert "endpoint is required" in output.lower()
        finally:
            sys.stderr = old_stderr

    def test_cmd_clear_specific(self):
        """Test clear command for specific endpoint."""
        with patch("smartratelimit.cli.RateLimiter") as mock_limiter_class:
            mock_limiter = mock_limiter_class.return_value

            old_stdout = sys.stdout
            sys.stdout = StringIO()

            try:
                cmd_clear(
                    type(
                        "Args",
                        (),
                        {"endpoint": "api.example.com", "storage": "memory"},
                    )()
                )
                output = sys.stdout.getvalue()
                assert "Cleared" in output
                mock_limiter.clear.assert_called_once_with("api.example.com")
            finally:
                sys.stdout = old_stdout

    def test_cmd_clear_all(self):
        """Test clear command for all endpoints."""
        with patch("smartratelimit.cli.RateLimiter") as mock_limiter_class:
            mock_limiter = mock_limiter_class.return_value

            old_stdout = sys.stdout
            sys.stdout = StringIO()

            try:
                cmd_clear(type("Args", (), {"endpoint": None, "storage": "memory"})())
                output = sys.stdout.getvalue()
                assert "Cleared all" in output
                mock_limiter.clear.assert_called_once_with(None)
            finally:
                sys.stdout = old_stdout

    def test_cmd_probe(self):
        """Test probe command."""
        mock_response = type(
            "Response",
            (),
            {
                "status_code": 200,
                "url": "https://api.github.com/users",
                "headers": {
                    "X-RateLimit-Limit": "5000",
                    "X-RateLimit-Remaining": "4999",
                    "X-RateLimit-Reset": "1640995200",
                },
            },
        )()

        mock_limiter = type("RateLimiter", (), {
            "request": lambda self, *args, **kwargs: mock_response,
            "get_status": lambda self, url: RateLimitStatus(
                endpoint="https://api.github.com",
                limit=5000,
                remaining=4999,
                window=timedelta(hours=1),
                confidence="estimated",
            )
        })()

        with patch("smartratelimit.cli.RateLimiter", return_value=mock_limiter):
            old_stdout = sys.stdout
            sys.stdout = StringIO()

            try:
                cmd_probe(
                    type(
                        "Args",
                        (),
                        {"url": "https://api.github.com/users", "storage": "memory"},
                    )()
                )
                output = sys.stdout.getvalue()
                assert "Response Status: 200" in output
                assert "X-RateLimit-Limit" in output
                # An assumed window must be labelled as one, not reported as
                # though the API had stated it.
                assert "Confidence: estimated" in output
                assert "assumption" in output
            finally:
                sys.stdout = old_stdout

    def test_main_no_command(self):
        """Test main with no command."""
        old_stdout = sys.stdout
        sys.stdout = StringIO()

        try:
            with patch("sys.argv", ["smartratelimit"]):
                with pytest.raises(SystemExit):
                    main()
        finally:
            sys.stdout = old_stdout

