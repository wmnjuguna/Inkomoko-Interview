"""The J-5 crash hook: kill the service right after a money operation's FINERACT_DONE
commit, before its journal entry reaches the ERP. For the failure simulation only.

Off unless FAULT_CRASH_AFTER_FINERACT_DONE is set, and its value is not a switch but an
external_reference prefix (for example "J5-"): only requests whose reference starts with it
crash. So it cannot be turned on by a stray "true", and other traffic on the same service
is never hit. Nothing in .env.example sets it; the J-5 script sets it for one run only.

The crash is a hard stop, like a power cut: no cleanup, no answer to the partner. Under
gunicorn it also kills the gunicorn master, so the whole container stops and compose
restarts it (restart: unless-stopped). Otherwise gunicorn would just start a new worker."""

from __future__ import annotations

import logging
import os
import signal

from integration_service.logs import log_event

CRASH_ENV = "FAULT_CRASH_AFTER_FINERACT_DONE"
CRASH_EXIT_CODE = 70


def crash_prefix() -> str | None:
    """The reference prefix the hook fires for, or None when it is off."""
    value = os.environ.get(CRASH_ENV, "").strip()
    return value or None


def crash_after_fineract_done(external_reference: str) -> None:
    """Called after FINERACT_DONE (with its outbox row) is committed. Returns at once unless
    the hook is on and the reference starts with its prefix."""
    prefix = crash_prefix()
    if prefix is None or not external_reference.startswith(prefix):
        return
    log_event(
        "FAULT INJECTION: crashing after FINERACT_DONE, before the ERP call",
        level=logging.CRITICAL,
        external_reference=external_reference,
        status="FINERACT_DONE",
    )
    _die()


def _die() -> None:
    parent = os.getppid()
    if _is_gunicorn(parent):
        os.kill(parent, signal.SIGKILL)
    os._exit(CRASH_EXIT_CODE)


def _is_gunicorn(pid: int) -> bool:
    try:
        with open(f"/proc/{pid}/cmdline", "rb") as handle:
            return b"gunicorn" in handle.read()
    except OSError:
        return False
