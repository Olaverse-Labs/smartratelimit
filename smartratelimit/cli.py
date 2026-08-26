"""CLI tools for smartratelimit."""

import argparse
import json
import sys
from typing import Optional

from smartratelimit import RateLimiter, RateLimitStatus


def cmd_status(args):
    """Display rate limit status for endpoint(s)."""
    limiter = RateLimiter(storage=args.storage)

    if args.endpoint:
        endpoints = [args.endpoint]
    else:
        # Get all endpoints from storage (if possible)
        # For now, just show the specified endpoint
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
            if status.confidence == "estimated":
                print(
                    "  Note: the API reported a limit but no reset time, so the "
                    "window above was assumed.\n"
                    "        Set the real one with RateLimiter.set_limit()."
                )
        else:
            print(f"\nEndpoint: {endpoint}")
            print("  No rate limit information available")


def cmd_clear(args):
    """Clear stored rate limit data."""
    limiter = RateLimiter(storage=args.storage)
    limiter.clear(args.endpoint)

    if args.endpoint:
        print(f"Cleared rate limit data for: {args.endpoint}")
    else:
        print("Cleared all rate limit data")


def cmd_probe(args):
    """Probe an endpoint to detect rate limits."""
    import requests

    limiter = RateLimiter(storage=args.storage)

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
    limiter = RateLimiter(storage=args.storage)

    # This is a simplified version - in a real implementation,
    # we'd need a way to list all endpoints from storage
    print("Note: Listing all endpoints requires storage backend support")
    print("Use 'smartratelimit status --endpoint <url>' to check specific endpoints")


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

    subparsers = parser.add_subparsers(dest="command", help="Command to run")

    # Status command
    status_parser = subparsers.add_parser("status", help="Show rate limit status")
    status_parser.add_argument(
        "endpoint", nargs="?", help="Endpoint URL or domain (optional)"
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

    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        sys.exit(1)

    args.func(args)


if __name__ == "__main__":
    main()

