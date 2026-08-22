#!/usr/bin/env python3
"""Deterministic callback delivery for the Jiffy sandbox.

This runs *inside* the sandbox container, not in the Gateway.  The agent
composes the comment body and hands it to this script; the script owns the
HTTP call, the retry policy, and the reporting of what happened.  None of
that is left to the agent's judgment — the agent's only job is to write the
body and copy the JSON this prints into its result file.

Standard library only: the sandbox image's Python has no third-party packages
installed, and the agent must not have to install one before it can report.

Usage:
    python3 jiffy_callback.py --body-file /path/to/body.md
                              [--config /tmp/jiffy_callback.json]

Exit codes:
    0  delivered
    1  not delivered (permanent rejection, or retries exhausted)
    2  the script could not run at all (bad config, unreadable body)

On stdout it prints exactly one JSON object matching the ``callback`` field of
the agent result contract:

    {"attempted": true, "succeeded": false, "error": "..."}

Progress goes to stderr, which the agent run redirects to the container's main
stdout, so every attempt is visible in ``docker logs``.
"""

import argparse
import json
import sys
import time
import urllib.error
import urllib.request

DEFAULT_CONFIG_PATH = "/tmp/jiffy_callback.json"
LOG_PREFIX = "[jiffy-callback]"

# Fallbacks only: the real values are written into the config file by the
# Gateway from jobs/callback_retry.py, which is the single source of truth.
FALLBACK_MAX_ATTEMPTS = 3
FALLBACK_BASE_DELAY_SECONDS = 2.0
FALLBACK_TIMEOUT_SECONDS = 30
FALLBACK_RETRYABLE_CLIENT_STATUSES = (408, 429)


def log(message):
    """Write a progress line to stderr, flushed so ordering survives piping."""
    sys.stderr.write("%s %s\n" % (LOG_PREFIX, message))
    sys.stderr.flush()


def is_transient_status(status_code, retryable_client_statuses):
    """True if *status_code* is worth another attempt.

    5xx plus the explicitly retryable client codes. Any other 4xx is a
    permanent rejection — a bad URL, a rejected secret, a deleted issue — and
    repeating the request cannot change the answer.
    """
    if status_code < 300:
        return False
    return status_code >= 500 or status_code in retryable_client_statuses


def build_wire_body(config, body_text):
    """Encode *body_text* the way this provider's endpoint expects it."""
    if config.get("body_format") == "json":
        field = config.get("body_text_field", "body")
        return json.dumps(
            {field: body_text}, separators=(",", ":"), ensure_ascii=False
        ).encode("utf-8")
    return body_text.encode("utf-8")


def send_once(config, wire_body, timeout):
    """One HTTP attempt.

    Returns ``(status_code, None)`` when the server answered — including with
    an error status — and ``(None, description)`` when the request never got
    an answer at all (DNS, connection refused, TLS, timeout).
    """
    request = urllib.request.Request(
        config["url"],
        data=wire_body,
        headers=config.get("headers", {}),
        method=config.get("method", "POST"),
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.getcode(), None
    except urllib.error.HTTPError as exc:
        # The server answered; the status decides whether it is worth retrying.
        return exc.code, None
    except urllib.error.URLError as exc:
        return None, "connection error: %s" % (exc.reason,)
    except Exception as exc:  # socket.timeout, ssl errors, anything else
        return None, "%s: %s" % (type(exc).__name__, exc)


def deliver(config, body_text):
    """Deliver the callback, retrying only what is worth retrying.

    Returns the ``callback`` object for the agent's result file.
    """
    max_attempts = int(config.get("max_attempts", FALLBACK_MAX_ATTEMPTS))
    base_delay = float(config.get("base_delay_seconds", FALLBACK_BASE_DELAY_SECONDS))
    timeout = float(config.get("timeout_seconds", FALLBACK_TIMEOUT_SECONDS))
    retryable = set(
        config.get("retryable_client_statuses", FALLBACK_RETRYABLE_CLIENT_STATUSES)
    )

    wire_body = build_wire_body(config, body_text)
    method = config.get("method", "POST")
    url = config["url"]
    last_error = "no attempt was made"

    for attempt in range(1, max_attempts + 1):
        if attempt > 1:
            delay = base_delay * (2 ** (attempt - 2))
            log("waiting %.1fs before attempt %d" % (delay, attempt))
            time.sleep(delay)

        log("attempt %d/%d: %s %s" % (attempt, max_attempts, method, url))
        status, transport_error = send_once(config, wire_body, timeout)

        if transport_error is not None:
            last_error = transport_error
            log("attempt %d/%d failed: %s" % (attempt, max_attempts, last_error))
        elif status < 300:
            log("delivered on attempt %d/%d (HTTP %d)" % (attempt, max_attempts, status))
            return {"attempted": True, "succeeded": True, "error": None}
        else:
            last_error = "HTTP %d" % (status,)
            log("attempt %d/%d returned HTTP %d" % (attempt, max_attempts, status))
            if not is_transient_status(status, retryable):
                # Permanent: stop now rather than repeating a request the
                # server has already refused on its merits.
                log("HTTP %d is a client error — not retrying" % (status,))
                return {
                    "attempted": True,
                    "succeeded": False,
                    "error": "%s (client error, not retried)" % (last_error,),
                }

    log("all %d attempts failed — giving up (last error: %s)" % (max_attempts, last_error))
    return {
        "attempted": True,
        "succeeded": False,
        "error": "all %d attempts failed; last error: %s" % (max_attempts, last_error),
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description="Deliver a Jiffy callback with retries.")
    parser.add_argument(
        "--body-file",
        required=True,
        help="File holding the markdown comment body to deliver.",
    )
    parser.add_argument(
        "--config",
        default=DEFAULT_CONFIG_PATH,
        help="Callback config staged by the Gateway (default: %s)." % DEFAULT_CONFIG_PATH,
    )
    args = parser.parse_args(argv)

    try:
        with open(args.config, "r", encoding="utf-8") as handle:
            config = json.load(handle)
    except Exception as exc:
        log("cannot read config %s: %s" % (args.config, exc))
        return 2

    if not config.get("url"):
        log("config %s has no callback URL" % (args.config,))
        return 2

    try:
        with open(args.body_file, "r", encoding="utf-8") as handle:
            body_text = handle.read()
    except Exception as exc:
        log("cannot read body file %s: %s" % (args.body_file, exc))
        return 2

    if not body_text.strip():
        log("body file %s is empty — refusing to post a blank comment" % (args.body_file,))
        return 2

    result = deliver(config, body_text)
    # The agent copies this object verbatim into .jiffy_result.json.
    sys.stdout.write(json.dumps(result) + "\n")
    sys.stdout.flush()
    return 0 if result["succeeded"] else 1


if __name__ == "__main__":
    sys.exit(main())
