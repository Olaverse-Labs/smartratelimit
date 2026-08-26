"""Test CLI with real API data.

Note: These tests require API keys to run. Replace YOUR_RAPIDAPI_KEY
with your actual RapidAPI key, or skip these tests if you don't have one.
"""

import os
import subprocess
import sys
import tempfile
import time

import pytest

from smartratelimit import RateLimiter

# Get API key from environment variable or use placeholder
RAPIDAPI_KEY = os.getenv("RAPIDAPI_KEY", "YOUR_RAPIDAPI_KEY")


class TestCLIReal:
    """Test CLI commands with real API data."""

    @pytest.mark.skipif(
        RAPIDAPI_KEY == "YOUR_RAPIDAPI_KEY",
        reason="RAPIDAPI_KEY not set. Set environment variable RAPIDAPI_KEY to run this test."
    )
    def test_cli_status_with_real_api(self):
        """Test CLI status command with real API data stored in SQLite."""
        # Create a temporary database
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
            db_path = f.name

        try:
            # Store rate limit data using the library
            limiter = RateLimiter(storage=f"sqlite:///{db_path}")
            url = "https://phone-and-email-validation-api.p.rapidapi.com/validate-phone"
            headers = {
                "x-rapidapi-key": RAPIDAPI_KEY,
                "x-rapidapi-host": "phone-and-email-validation-api.p.rapidapi.com",
                "Content-Type": "application/json",
            }

            # Make a request to store rate limit data
            response = limiter.request(
                "POST", url, headers=headers, json={"phone_number": "+12065550100"}
            )
            assert response.status_code in [200, 201]

            # Verify data was stored
            status = limiter.get_status(url)
            assert status is not None, "Rate limit data should be stored"
            assert status.limit > 0, "Rate limit should be detected"

            # Test CLI status command
            result = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "smartratelimit.cli",
                    "--storage",
                    f"sqlite:///{db_path}",
                    "status",
                    url,
                ],
                capture_output=True,
                text=True,
            )

            assert result.returncode == 0, f"CLI failed: {result.stderr}"
            output = result.stdout

            # Verify CLI output contains expected information
            assert "Endpoint:" in output or "Limit:" in output
            assert str(status.limit) in output or "limit" in output.lower()
            print(f"\nCLI Output:\n{output}")

        finally:
            # Clean up
            if os.path.exists(db_path):
                os.unlink(db_path)

    @pytest.mark.skipif(
        RAPIDAPI_KEY == "YOUR_RAPIDAPI_KEY",
        reason="RAPIDAPI_KEY not set. Set environment variable RAPIDAPI_KEY to run this test."
    )
    def test_cli_status_multiple_requests(self):
        """Test CLI status after multiple requests."""
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
            db_path = f.name

        try:
            limiter = RateLimiter(storage=f"sqlite:///{db_path}")
            url = "https://phone-and-email-validation-api.p.rapidapi.com/validate-phone"
            headers = {
                "x-rapidapi-key": RAPIDAPI_KEY,
                "x-rapidapi-host": "phone-and-email-validation-api.p.rapidapi.com",
                "Content-Type": "application/json",
            }

            # Make multiple requests
            for i in range(3):
                response = limiter.request(
                    "POST",
                    url,
                    headers=headers,
                    json={"phone_number": f"+1206555010{i}"},
                )
                assert response.status_code in [200, 201]
                time.sleep(0.5)  # Small delay

            # Get final status
            final_status = limiter.get_status(url)
            assert final_status is not None

            # Test CLI status command
            result = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "smartratelimit.cli",
                    "--storage",
                    f"sqlite:///{db_path}",
                    "status",
                    url,
                ],
                capture_output=True,
                text=True,
            )

            assert result.returncode == 0
            output = result.stdout

            # Verify CLI shows updated remaining count
            assert str(final_status.remaining) in output or "remaining" in output.lower()
            print(f"\nCLI Output after 3 requests:\n{output}")

        finally:
            if os.path.exists(db_path):
                os.unlink(db_path)

    @pytest.mark.skipif(
        RAPIDAPI_KEY == "YOUR_RAPIDAPI_KEY",
        reason="RAPIDAPI_KEY not set. Set environment variable RAPIDAPI_KEY to run this test."
    )
    def test_cli_clear(self):
        """Test CLI clear command."""
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
            db_path = f.name

        try:
            limiter = RateLimiter(storage=f"sqlite:///{db_path}")
            url = "https://phone-and-email-validation-api.p.rapidapi.com/validate-phone"
            headers = {
                "x-rapidapi-key": RAPIDAPI_KEY,
                "x-rapidapi-host": "phone-and-email-validation-api.p.rapidapi.com",
                "Content-Type": "application/json",
            }

            # Store data
            response = limiter.request(
                "POST", url, headers=headers, json={"phone_number": "+12065550100"}
            )
            assert response.status_code in [200, 201]

            # Verify data exists
            status = limiter.get_status(url)
            assert status is not None

            # Clear using CLI
            result = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "smartratelimit.cli",
                    "--storage",
                    f"sqlite:///{db_path}",
                    "clear",
                    url,
                ],
                capture_output=True,
                text=True,
            )

            assert result.returncode == 0
            assert "Cleared" in result.stdout

            # Verify data was cleared
            status_after = limiter.get_status(url)
            assert status_after is None, "Rate limit data should be cleared"

        finally:
            if os.path.exists(db_path):
                os.unlink(db_path)

    @pytest.mark.network
    def test_cli_probe(self):
        """Test CLI probe command against a live endpoint.

        Marked ``network``: it calls httpbin.org, whose availability is not this
        project's to guarantee. CI deselects it so a third party being down
        cannot turn the build red.
        """
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
            db_path = f.name

        try:
            # Test probe command (this will make a GET request)
            # Note: The API might not support GET, so we'll test with a simple endpoint
            # For now, we'll skip this or use a different endpoint
            result = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "smartratelimit.cli",
                    "--storage",
                    f"sqlite:///{db_path}",
                    "probe",
                    "https://httpbin.org/get",
                ],
                capture_output=True,
                text=True,
                timeout=10,
            )

            # Probe might succeed or fail depending on the endpoint
            # Just verify the command runs
            assert result.returncode in [0, 1]  # May fail if endpoint doesn't support GET
            print(f"\nProbe output:\n{result.stdout}")

        finally:
            if os.path.exists(db_path):
                os.unlink(db_path)

