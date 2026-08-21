"""Startup banner printed once when the web server boots."""

import os
import sys

from django.conf import settings


def _is_web_server() -> bool:
    """True only for the process that actually serves HTTP."""
    argv = " ".join(sys.argv)
    if "gunicorn" in argv or "uvicorn" in argv or "daphne" in argv:
        return True
    if "runserver" not in argv:
        return False
    # With the autoreloader the parent process also imports the apps; only the
    # reloaded child has RUN_MAIN set. Without --noreload there is no child.
    return os.environ.get("RUN_MAIN") == "true" or "--noreload" in sys.argv


def _bind_address() -> tuple[str, str]:
    """Best-effort host/port the server is listening on, for the printed URLs."""
    port = os.environ.get("PORT", "8000")
    for arg in sys.argv[1:]:
        if arg.startswith("-"):
            continue
        if ":" in arg:
            _, _, maybe_port = arg.rpartition(":")
            if maybe_port.isdigit():
                port = maybe_port
        elif arg.isdigit():
            port = arg

    allowed_hosts = getattr(settings, "ALLOWED_HOSTS", ["localhost"])
    host = allowed_hosts[0] if allowed_hosts else "localhost"
    if host in ("*", "0.0.0.0", ""):
        host = "localhost"
    return host, port


def _title() -> str:
    """The rocket, unless stdout is on a legacy codepage that cannot encode it."""
    encoding = getattr(sys.stdout, "encoding", None) or "ascii"
    try:
        "🚀".encode(encoding)
    except (UnicodeEncodeError, LookupError):
        return "*"
    return "🚀"


def print_startup_banner() -> None:
    if not _is_web_server():
        return

    version = getattr(settings, "SPECTACULAR_SETTINGS", {}).get("VERSION", "unknown")
    host, port = _bind_address()

    print("=" * 50)
    print(f"{_title()} Application Started")
    print(f"   Version:    {version}")
    print(f"   Swagger:    http://{host}:{port}/api/docs/")
    print(f"   Redoc:      http://{host}:{port}/api/redoc/")
    print(f"   API Schema: http://{host}:{port}/api/schema/")
    print("=" * 50)
