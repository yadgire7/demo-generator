"""A worker process for the service tests: every slow stage is a stand-in, and one can be told to hang.

Run as: python -m tests.service_worker <pool> [<pool> ...]. Reads the service's settings from the environment.
"""

from __future__ import annotations

import os
import sys
import time
from dataclasses import replace

from demogen.service.core import Service
from demogen.service.settings import Settings
from tests.conftest import all_fakes, fake_rehearse


def hanging_rehearse(ctx, inputs):
    """Hang for as long as the flag file exists: a stage whose worker can be killed mid-way."""
    while os.path.exists(os.environ["DEMOGEN_TEST_HANG_FLAG"]):
        time.sleep(0.05)
    return fake_rehearse(ctx, inputs)


if __name__ == "__main__":
    registry = all_fakes()
    if os.environ.get("DEMOGEN_TEST_HANG_FLAG"):
        registry["rehearse"] = replace(registry["rehearse"], fn=hanging_rehearse)
    service = Service(Settings.from_env(), registry, heartbeat_s=0.3, stalled_after_s=1.5)
    service.run_worker(sys.argv[1:], concurrency=6)
