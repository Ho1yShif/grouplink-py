"""Logging setup for the two entry points.

Neither the Render SDK nor render-lab-triggers configures logging, and Python's
default root level is WARNING, so every `log.info` in this repo is dropped unless
something calls this. The deploy steps read two of those lines out of the service
logs: the Notion `verification_token` during the webhook handshake, and the
unrecognized `Icon` options after a rebuild.
"""

from __future__ import annotations

import logging


def configure() -> None:
    """Send INFO and above to stderr, which Render captures as service logs.

    `force` replaces a handler an imported library installed, so the format below is
    the one that reaches the logs.
    """
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        force=True,
    )
