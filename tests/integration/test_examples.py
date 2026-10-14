"""The examples that ship with the project keep working."""

from __future__ import annotations

import threading
from functools import partial
from http.server import ThreadingHTTPServer

import pytest
from playwright.sync_api import expect, sync_playwright

from demogen.playbook.loader import parse_playbook
from tests.conftest import ROOT, _QuietHandler

EXAMPLES = ROOT.parent / "examples"


@pytest.mark.parametrize("path", sorted((EXAMPLES / "playbooks").glob("*.yaml")), ids=lambda path: path.name)
def test_example_playbooks_are_valid(path):
    assert parse_playbook(path.read_text()).steps


@pytest.fixture
def shop():
    server = ThreadingHTTPServer(("127.0.0.1", 0), partial(_QuietHandler, directory=str(EXAMPLES / "shop")))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        page = browser.new_context(viewport={"width": 1440, "height": 900}).new_page()
        errors: list[str] = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.goto(f"http://127.0.0.1:{server.server_port}/")
        yield page
        assert errors == []  # the store's own script never throws
        browser.close()
    server.shutdown()


def test_sample_store_search_options_and_cart(shop):
    page = shop
    expect(page.get_by_role("link", name="Crewneck T-Shirt, $24.00")).to_be_visible()
    expect(page.locator(".card")).to_have_count(6)

    page.get_by_role("searchbox", name="Search products").fill("t-shirt")
    page.get_by_role("button", name="Search").click()
    expect(page.get_by_text("2 products found")).to_be_visible()

    page.get_by_role("link", name="Crewneck T-Shirt, $24.00").click()
    add = page.get_by_role("button", name="Add to cart")
    expect(add).to_be_disabled()
    expect(page.get_by_text("Choose a size to add this to your cart.")).to_be_visible()
    # Choosing a colour changes the label and the picture; it opens in grey so that Navy is a visible change.
    expect(page.get_by_text("Colour: Heather Grey")).to_be_visible()
    page.get_by_role("radio", name="Navy").click()
    expect(page.get_by_text("Colour: Navy")).to_be_visible()
    expect(page.get_by_role("img", name="Crewneck T-Shirt in Navy")).to_be_visible()
    page.get_by_role("radio", name="L", exact=True).click()
    page.get_by_role("button", name="One more").click()
    add.click()
    expect(page.locator("#toast")).to_have_text("Added to cart: Crewneck T-Shirt, Navy, L")
    expect(page.locator("#badge")).to_have_text("2")

    page.get_by_role("link", name="Cart").click()
    expect(page.get_by_text("Navy · Size L")).to_be_visible()
    expect(page.locator(".total")).to_have_text("$48.00")
    page.reload()  # the cart survives a reload
    expect(page.locator(".total")).to_have_text("$48.00")
    page.get_by_role("button", name="Remove Crewneck T-Shirt").click()
    expect(page.get_by_text("Your cart is empty.")).to_be_visible()
    expect(page.locator("#badge")).to_have_text("0")


def test_sample_store_marks_sold_out_sizes_and_handles_no_results(shop):
    page = shop
    page.get_by_role("searchbox", name="Search products").fill("umbrella")
    page.keyboard.press("Enter")
    expect(page.get_by_text("Nothing matches “umbrella”.")).to_be_visible()

    page.goto(page.url.split("#")[0] + "#/product/crewneck-t-shirt")
    page.get_by_role("radio", name="Forest").click()
    expect(page.get_by_role("radio", name="XXL (sold out)")).to_be_disabled()
    page.get_by_role("radio", name="XL", exact=True).click()
    page.get_by_role("radio", name="White").click()  # XL exists in white too, so the choice is kept
    expect(page.get_by_text("Size: XL")).to_be_visible()

    page.goto(page.url.split("#")[0] + "#/product/canvas-tote")
    expect(page.get_by_role("button", name="Add to cart")).to_be_enabled()  # one size: nothing to choose first
