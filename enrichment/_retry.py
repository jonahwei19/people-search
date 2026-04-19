"""Retry helpers for transient network errors.

Used by enrichers.py (EnrichLayer) and identity.py (Brave/Serper/page fetch).
Retries on timeouts, connection errors, and 5xx responses — NOT on 4xx
(the caller handles those meaningfully: 402=OUT_OF_CREDITS, 404=not found,
429=rate-limit with its own retry-after header).
"""

from __future__ import annotations

import random
import sys
import time
from typing import Callable, TypeVar

import requests

T = TypeVar("T")

# Which exception types are "retryable" — transient network issues
TRANSIENT_EXCEPTIONS = (
    requests.Timeout,
    requests.ConnectionError,
    requests.exceptions.ChunkedEncodingError,
)


def retry_request(
    fn: Callable[[], T],
    *,
    max_attempts: int = 3,
    base_delay: float = 2.0,
    max_delay: float = 30.0,
    label: str = "request",
    on_5xx: bool = True,
) -> T | None:
    """Run `fn()` with exponential backoff on transient failures.

    `fn` should raise `requests.Timeout` / `requests.ConnectionError` on
    transient issues, or return a `requests.Response` so we can inspect
    status_code. Returns the successful result, or None after exhaustion.

    Backoff: 2s, 4s, 8s with 25% jitter, capped at `max_delay`.

    The caller is still responsible for handling 4xx status codes — this
    helper only retries network errors and (optionally) 5xx. For 429
    rate-limits use `Retry-After` yourself; we don't second-guess.
    """
    delay = base_delay
    last_err: Exception | None = None
    for attempt in range(1, max_attempts + 1):
        try:
            result = fn()
        except TRANSIENT_EXCEPTIONS as e:
            last_err = e
            if attempt >= max_attempts:
                print(
                    f"  [{label}] gave up after {attempt} attempts: {e.__class__.__name__}",
                    file=sys.stderr,
                )
                return None
            sleep_s = min(delay, max_delay) * (1 + random.uniform(-0.25, 0.25))
            print(
                f"  [{label}] transient error ({e.__class__.__name__}); "
                f"retry {attempt}/{max_attempts - 1} in {sleep_s:.1f}s",
                file=sys.stderr,
            )
            time.sleep(sleep_s)
            delay *= 2
            continue
        except Exception as e:
            # Non-transient — don't retry
            print(f"  [{label}] non-retryable: {e}", file=sys.stderr)
            return None

        # Got a response. Retry 5xx if requested.
        if on_5xx and isinstance(result, requests.Response) and 500 <= result.status_code < 600:
            if attempt >= max_attempts:
                print(
                    f"  [{label}] {result.status_code} persisted across {attempt} attempts",
                    file=sys.stderr,
                )
                return result
            sleep_s = min(delay, max_delay) * (1 + random.uniform(-0.25, 0.25))
            print(
                f"  [{label}] HTTP {result.status_code}; retry {attempt}/{max_attempts - 1} in {sleep_s:.1f}s",
                file=sys.stderr,
            )
            time.sleep(sleep_s)
            delay *= 2
            continue

        return result

    # Exhausted without return — last_err was set
    if last_err is not None:
        print(f"  [{label}] exhausted. Last error: {last_err}", file=sys.stderr)
    return None
