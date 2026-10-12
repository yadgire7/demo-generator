"""Record a page by streaming Chrome's screencast frames into ffmpeg.

Chrome sends a frame only when the page repaints, each stamped with its capture time. The
frames are laid onto a constant-rate timeline by repeating the previous frame across any gap,
so the video's clock matches the wall clock the event log is written against.
"""

from __future__ import annotations

import base64
import subprocess
import time
from pathlib import Path
from typing import Any

from playwright.sync_api import BrowserContext, Page

from demogen.errors import DemogenError

FIRST_FRAME_TIMEOUT_S = 5.0
JPEG_QUALITY = 90


class RecorderError(DemogenError):
    pass


class CdpScreencastRecorder:
    def __init__(self, *, fps: int = 30, crf: int = 18):
        self.fps = fps
        self.crf = crf
        self._started_at: float | None = None  # capture time of the first frame; the take's t=0
        self._pending: bytes | None = None  # newest frame, not yet written
        self._written = 0
        self._closed = False  # set once frames should no longer be written
        self._failure: RecorderError | None = None

    def start(self, context: BrowserContext, page: Page, out: Path, size: tuple[int, int]) -> None:
        self._page = page
        self._ffmpeg = subprocess.Popen(
            [
                "ffmpeg", "-y", "-loglevel", "error",
                "-f", "image2pipe", "-c:v", "mjpeg", "-framerate", str(self.fps), "-i", "-",
                "-c:v", "libx264", "-preset", "veryfast", "-crf", str(self.crf),
                "-vf", "scale=trunc(iw/2)*2:trunc(ih/2)*2", "-pix_fmt", "yuv420p", "-movflags", "+faststart",
                str(out),
            ],  # fmt: skip
            stdin=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        self._cdp = context.new_cdp_session(page)
        self._cdp.on("Page.screencastFrame", self._on_frame)
        self._cdp.send(
            "Page.startScreencast",
            {"format": "jpeg", "quality": JPEG_QUALITY, "maxWidth": size[0], "maxHeight": size[1], "everyNthFrame": 1},
        )
        deadline = time.time() + FIRST_FRAME_TIMEOUT_S
        while self._started_at is None:
            if time.time() > deadline:
                self._ffmpeg.kill()
                raise RecorderError("the browser sent no video frames")
            page.wait_for_timeout(20)  # lets Playwright deliver the frame event

    def now(self) -> float:
        """Seconds since the first video frame: the clock every take event is stamped with."""
        return time.time() - self._started_at

    def _on_frame(self, params: dict[str, Any]) -> None:
        # This runs inside Playwright's event dispatch, where an exception would wedge the
        # browser connection. A failure is kept for `stop` to raise; late frames are dropped.
        if self._closed:
            return
        captured_at = params["metadata"]["timestamp"]
        if self._started_at is None:
            self._started_at = captured_at
        else:
            try:
                self._write_until(captured_at)
            except RecorderError as error:
                self._failure, self._closed = error, True
                return
        self._pending = base64.b64decode(params["data"])
        self._cdp.send("Page.screencastFrameAck", {"sessionId": params["sessionId"]})

    def _write_until(self, moment: float, at_least: int = 0) -> None:
        """Hold the pending frame on screen up to `moment`."""
        due = max(at_least, round((moment - self._started_at) * self.fps) - self._written)
        try:
            for _ in range(due):
                self._ffmpeg.stdin.write(self._pending)
        except BrokenPipeError as error:
            raise RecorderError(f"ffmpeg stopped: {self._ffmpeg.stderr.read().decode().strip()}") from error
        self._written += due

    def stop(self) -> float:
        """Finish the file. Returns the video's duration in seconds."""
        self._page.wait_for_timeout(50)  # collect frames still in flight
        self._cdp.send("Page.stopScreencast")
        self._closed = True
        if self._failure:
            self.abort()
            raise self._failure
        self._write_until(time.time(), at_least=1)
        self._ffmpeg.stdin.close()
        if self._ffmpeg.wait(timeout=120) != 0:
            raise RecorderError(f"ffmpeg failed: {self._ffmpeg.stderr.read().decode().strip()}")
        return self._written / self.fps

    def abort(self) -> None:
        """Drop a recording that will not be used."""
        self._closed = True
        if self._ffmpeg.poll() is None:
            self._ffmpeg.kill()
            self._ffmpeg.wait()
