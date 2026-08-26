"""Advanced retry logic for rate-limited requests."""

import asyncio
import logging
import random
import time
from enum import Enum
from typing import Callable, Optional, TypeVar, Union

logger = logging.getLogger(__name__)

T = TypeVar("T")


class RetryStrategy(Enum):
    """Retry strategies for handling rate limits."""

    EXPONENTIAL = "exponential"
    LINEAR = "linear"
    FIXED = "fixed"
    NONE = "none"


class RetryConfig:
    """Configuration for retry behavior."""

    def __init__(
        self,
        max_retries: int = 3,
        strategy: RetryStrategy = RetryStrategy.EXPONENTIAL,
        base_delay: float = 1.0,
        max_delay: float = 60.0,
        backoff_factor: float = 2.0,
        retry_on_status: Optional[list] = None,
        jitter: float = 0.0,
    ):
        """
        Initialize retry configuration.

        Args:
            max_retries: Maximum number of retry attempts
            strategy: Retry strategy (exponential, linear, fixed, none)
            base_delay: Base delay in seconds for retries
            max_delay: Maximum delay in seconds between retries
            backoff_factor: Factor for exponential backoff
            retry_on_status: HTTP status codes to retry on (default: [429, 503, 504])
            jitter: Fraction of the delay to randomise, 0.0-1.0. Without it,
                every client rate-limited by the same window retries at the
                same instant and collides again; 0.1 spreads them out by up
                to 10%. Defaults to 0.0 so delays stay exactly predictable
                unless jitter is asked for.
        """
        self.max_retries = max_retries
        self.strategy = strategy
        self.base_delay = base_delay
        self.max_delay = max_delay
        self.backoff_factor = backoff_factor
        self.retry_on_status = retry_on_status or [429, 503, 504]
        self.jitter = jitter


class RetryHandler:
    """Handler for retrying requests with various strategies."""

    def __init__(self, config: Optional[RetryConfig] = None):
        """
        Initialize retry handler.

        Args:
            config: Retry configuration (uses defaults if None)
        """
        self.config = config or RetryConfig()

    def _calculate_delay(self, attempt: int) -> float:
        """Calculate delay for retry attempt."""
        if self.config.strategy == RetryStrategy.NONE:
            return 0.0

        if self.config.strategy == RetryStrategy.FIXED:
            delay = self.config.base_delay
        elif self.config.strategy == RetryStrategy.LINEAR:
            delay = self.config.base_delay * attempt
        elif self.config.strategy == RetryStrategy.EXPONENTIAL:
            delay = self.config.base_delay * (
                self.config.backoff_factor ** (attempt - 1)
            )
        else:
            delay = self.config.base_delay

        delay = min(delay, self.config.max_delay)
        return self._apply_jitter(delay)

    def _apply_jitter(self, delay: float) -> float:
        """Randomise a delay by up to ``config.jitter`` in either direction."""
        if not self.config.jitter or delay <= 0:
            return delay

        spread = delay * self.config.jitter
        return max(0.0, delay + random.uniform(-spread, spread))

    def delay_for_attempt(self, attempt: int) -> float:
        """
        Seconds to wait before ``attempt`` (1-based), per the configured strategy.

        Args:
            attempt: Which retry this is -- 1 for the first retry.
        """
        return self._calculate_delay(attempt)

    def max_attempts(self) -> int:
        """
        Total attempts allowed, including the first.

        ``RetryStrategy.NONE`` means exactly one attempt: retrying with a zero
        delay would just hammer an endpoint that already said no.
        """
        if self.config.strategy == RetryStrategy.NONE:
            return 1
        return self.config.max_retries + 1

    def should_retry(self, status_code: int, attempt: int) -> bool:
        """Determine if request should be retried."""
        if attempt > self.config.max_retries:
            return False

        if status_code in self.config.retry_on_status:
            return True

        return False

    def retry_sync(
        self, func: Callable[[], T], *args, **kwargs
    ) -> T:
        """
        Retry a synchronous function.

        Args:
            func: Function to retry
            *args: Positional arguments for function
            **kwargs: Keyword arguments for function

        Returns:
            Result of function call

        Raises:
            Exception: Last exception if all retries fail
        """
        last_exception = None
        attempt = 0

        while attempt <= self.config.max_retries:
            try:
                result = func(*args, **kwargs)

                # Check if result has status_code attribute (HTTP response)
                if hasattr(result, "status_code"):
                    status_code = result.status_code
                    if not self.should_retry(status_code, attempt + 1):
                        return result

                    if attempt < self.config.max_retries:
                        delay = self._calculate_delay(attempt + 1)
                        logger.info(
                            f"Retrying after {delay:.2f}s (attempt {attempt + 1}/{self.config.max_retries})"
                        )
                        time.sleep(delay)
                        attempt += 1
                        continue

                return result

            except Exception as e:
                last_exception = e
                if attempt < self.config.max_retries:
                    delay = self._calculate_delay(attempt + 1)
                    logger.warning(
                        f"Request failed: {e}. Retrying after {delay:.2f}s (attempt {attempt + 1}/{self.config.max_retries})"
                    )
                    time.sleep(delay)
                    attempt += 1
                else:
                    break

        if last_exception:
            raise last_exception
        return result

    async def retry_async(
        self, func: Callable, *args, **kwargs
    ) -> T:
        """
        Retry an async function.

        Args:
            func: Async function to retry
            *args: Positional arguments for function
            **kwargs: Keyword arguments for function

        Returns:
            Result of async function call

        Raises:
            Exception: Last exception if all retries fail
        """
        last_exception = None
        attempt = 0

        while attempt <= self.config.max_retries:
            try:
                result = await func(*args, **kwargs)

                # Check if result has status_code or status attribute
                status_code = None
                if hasattr(result, "status_code"):
                    status_code = result.status_code
                elif hasattr(result, "status"):
                    status_code = result.status

                if status_code and not self.should_retry(status_code, attempt + 1):
                    return result

                if attempt < self.config.max_retries:
                    delay = self._calculate_delay(attempt + 1)
                    logger.info(
                        f"Retrying after {delay:.2f}s (attempt {attempt + 1}/{self.config.max_retries})"
                    )
                    await asyncio.sleep(delay)
                    attempt += 1
                    continue

                return result

            except Exception as e:
                last_exception = e
                if attempt < self.config.max_retries:
                    delay = self._calculate_delay(attempt + 1)
                    logger.warning(
                        f"Request failed: {e}. Retrying after {delay:.2f}s (attempt {attempt + 1}/{self.config.max_retries})"
                    )
                    await asyncio.sleep(delay)
                    attempt += 1
                else:
                    break

        if last_exception:
            raise last_exception
        return result

