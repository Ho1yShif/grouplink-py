"""The logging setup the deploy steps depend on.

Python's default root level is WARNING and neither the Render SDK nor
render-lab-triggers changes it, so without `configure` the Notion handshake token
and the rebuild's skipped rows never reach the service logs.
"""

from __future__ import annotations

import logging
import subprocess
import sys

import pytest

from grouplink.logs import configure


@pytest.fixture(autouse=True)
def restore_root_logger():
    root = logging.getLogger()
    level, handlers = root.level, root.handlers[:]
    yield
    root.setLevel(level)
    root.handlers[:] = handlers


def test_lets_an_info_record_through_to_a_handler() -> None:
    configure()

    root = logging.getLogger()
    assert root.handlers, "configure installs a handler"
    assert logging.getLogger("grouplink.notion_webhook").isEnabledFor(logging.INFO)


def test_importing_the_workflow_entry_point_configures_logging() -> None:
    """`render-workflows grouplink.main:app` imports the module rather than running
    it, so the configuration cannot hide under `__main__`. A subprocess, because the
    import registers tasks and cannot be repeated in this one.
    """
    probe = (
        "import logging, grouplink.main; "
        "print(logging.getLogger('grouplink.rebuild').isEnabledFor(logging.INFO))"
    )
    result = subprocess.run(
        [sys.executable, "-c", probe], capture_output=True, text=True, check=True
    )

    assert result.stdout.strip() == "True", result.stderr
