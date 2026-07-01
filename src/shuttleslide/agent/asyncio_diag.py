"""Permanent asyncio noise filter — silences Windows
``ProactorEventLoop`` WinError-10054 "Exception in callback" spam.

Background
----------
On Windows + Python's ``ProactorEventLoop`` (the default since 3.8),
when a remote peer resets a socket (TCP RST), the transport's
``_call_connection_lost`` callback still attempts
``socket.shutdown(SHUT_RDWR)`` on the already-reset fd and raises
``ConnectionResetError: [WinError 10054]``. asyncio has no targeted
handler for this and dumps the full traceback on stderr as
"Exception in callback" noise.

Source identified
-----------------
The noise is always a normal client-disconnect side effect — the
peer addresses seen in production are local high ports (browser WS
clients of the review server, Playwright's transient RPC sockets,
httpx connection pool churn). TCP RST on close is benign; the
Python asyncio team treats this as a known issue
(https://github.com/python/cpython/issues/120749) and has no plan
to silence it inside the library.

So we drop these exceptions silently at the handler level. Other
exceptions still fall through to ``loop.default_exception_handler``
unchanged.

History: this module was originally a diagnostic that logged the
offending transport's peer address. Once source was confirmed to be
normal client disconnects, the peer-logging block was dropped.
"""

from __future__ import annotations

import asyncio


def install_noise_filter(loop: asyncio.AbstractEventLoop) -> None:
    """Silently swallow WinError-10054 ``ConnectionResetError`` on ``loop``.

    All other exceptions defer to ``loop.default_exception_handler`` so
    real bugs still surface with full context.
    """

    def handler(loop: asyncio.AbstractEventLoop, context: dict) -> None:
        exc = context.get("exception")
        # ``exc.winerror`` is set by Python on Windows for socket errors.
        # ``exc.errno`` is the cross-platform field (10054 on Windows).
        winerror = getattr(exc, "winerror", None) or getattr(exc, "errno", None)
        if isinstance(exc, ConnectionResetError) and winerror == 10054:
            # Silent swallow — see module docstring for why this is safe.
            return
        loop.default_exception_handler(context)

    loop.set_exception_handler(handler)
