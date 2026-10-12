"""The screen recorder on its own, against a page that never stops repainting."""

from __future__ import annotations

import pytest
from playwright.sync_api import sync_playwright

from demogen import media
from demogen.recorder.cdp_screencast import CdpScreencastRecorder

ANIMATED = """<body style="margin:0"><div id="b" style="width:120px;height:120px;background:#c33;position:absolute"></div>
<script>let x = 0; (function tick() { x = (x + 7) % 600; b.style.left = x + 'px'; requestAnimationFrame(tick); })();</script></body>"""


@pytest.fixture
def animated_page():
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        context = browser.new_context(viewport={"width": 800, "height": 600})
        page = context.new_page()
        page.set_content(ANIMATED)
        yield context, page
        browser.close()


def test_recording_keeps_wall_clock_time(animated_page, tmp_path):
    context, page = animated_page
    recorder = CdpScreencastRecorder(fps=30)
    recorder.start(context, page, tmp_path / "out.mp4", (800, 600))
    page.wait_for_timeout(1500)
    elapsed = recorder.now()
    seconds = recorder.stop()
    info = media.probe(tmp_path / "out.mp4")
    assert seconds == pytest.approx(elapsed, abs=0.15) and info.seconds == pytest.approx(seconds, abs=0.05)
    assert (info.width, info.height, info.fps) == (800, 600, 30)


def test_aborting_mid_animation_leaves_the_browser_usable(animated_page, tmp_path):
    context, page = animated_page
    recorder = CdpScreencastRecorder()
    recorder.start(context, page, tmp_path / "out.mp4", (800, 600))
    page.wait_for_timeout(200)
    recorder.abort()
    page.wait_for_timeout(300)  # frames that were still in flight are delivered now
    assert page.evaluate("1 + 1") == 2
