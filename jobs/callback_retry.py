"""Retry policy for outbound callback delivery.

One policy, two callers: the Gateway's fallback sender
(``apps.ingestion.callback``) and the deterministic wrapper the sandbox runs
(``jobs/execution/sandbox_scripts/jiffy_callback.py``).  The wrapper cannot
import this module — it runs inside the container with only the standard
library — so the values are handed to it in its config file, staged from here.
That keeps the two sides from drifting apart.

Attempt count follows the convention already used elsewhere in the Gateway
(``execute_task``'s ``max_retries=3``, the callback sender's ``MAX_RETRIES``),
and the backoff starts from the sender's existing 2-second delay.
"""

# Total attempts, not retries-after-the-first: 3 means one try plus two retries.
MAX_ATTEMPTS = 3

# First backoff, doubling per attempt: 2s, then 4s. Worst case adds 6s of
# waiting on top of the request timeouts, which is well inside the agent run
# budget (``SANDBOX_AGENT_TIMEOUT``) and the container's own lifetime.
BASE_DELAY_SECONDS = 2.0

# Per-request ceiling.  Bounded attempts and a bounded per-request timeout are
# what stop a stuck endpoint from hanging the caller: nothing here waits
# forever, so an unreachable callback ends as a logged failure and leaves the
# Gateway-side run timeout as the final safety net.
TIMEOUT_SECONDS = 30

# Status codes that are worth trying again even though they are below 500:
# the server is telling us it is busy or the request timed out on its side.
RETRYABLE_CLIENT_STATUSES = frozenset({408, 429})


def is_success(status_code: int) -> bool:
    """True if *status_code* means the callback was delivered."""
    return status_code < 300


def is_transient_status(status_code: int) -> bool:
    """True if *status_code* is worth retrying.

    5xx, plus the two 4xx codes that mean "try again". Every other client
    error is the caller's fault — a bad URL, a rejected secret, a deleted
    issue — and repeating the request cannot change the answer, so it must
    fail immediately rather than burn the retry budget.
    """
    if is_success(status_code):
        return False
    return status_code >= 500 or status_code in RETRYABLE_CLIENT_STATUSES


def delay_before_attempt(attempt: int, base_delay: float = BASE_DELAY_SECONDS) -> float:
    """Seconds to wait before *attempt* (1-based); 0 before the first."""
    if attempt <= 1:
        return 0.0
    return base_delay * (2 ** (attempt - 2))


def policy_for_sandbox() -> dict:
    """The policy as the sandbox wrapper's config file carries it."""
    return {
        "max_attempts": MAX_ATTEMPTS,
        "base_delay_seconds": BASE_DELAY_SECONDS,
        "timeout_seconds": TIMEOUT_SECONDS,
        "retryable_client_statuses": sorted(RETRYABLE_CLIENT_STATUSES),
    }
