"""Larkspur Market, the test shop: its pages and flows, a purchase in a real browser, and a demogen take of one."""

from __future__ import annotations

import re
import threading
import time

import pytest
import uvicorn
import yaml
from fastapi.testclient import TestClient
from playwright.sync_api import expect, sync_playwright

from demogen.testshop import create_shop
from demogen.testshop.catalog import PRODUCTS, by_category, search
from tests.conftest import _free_port

CARD = "4242 4242 4242 4242"
DECLINED = "4000 0000 0000 0002"
ADDRESS = {"name": "Sam Rivera", "line1": "4 Elm Street", "line2": "", "city": "Springfield", "postcode": "62701", "country": "United States"}


@pytest.fixture
def shop():
    return TestClient(create_shop(), base_url="http://127.0.0.1:8400")


def _text_of(html: str) -> str:
    """A page's visible words, tags removed and spaces collapsed."""
    return " ".join(re.sub(r"<[^>]+>", " ", re.sub(r"<(script|style)[^>]*>.*?</\1>", "", html, flags=re.S)).split())


def _text(response) -> str:
    return _text_of(response.text)


def _buy(client, slug="crewneck-t-shirt", colour="Navy", size="L", quantity=1):
    return client.post("/cart/add", data={"slug": slug, "colour": colour, "size": size, "quantity": quantity})


def _checkout(client, card=CARD, email="sam@example.com"):
    client.post("/checkout/shipping", data={"email": email, "address": "new", "delivery": "standard", **ADDRESS})
    client.post("/checkout/payment", data={"card_name": "Sam Rivera", "card_number": card, "expiry": "12/39", "cvc": "123"})
    return client.post("/checkout/place")


def test_search_finds_whole_words_and_shows_the_colour_asked_for(shop):
    assert [p.slug for p in search("navy t-shirt")] == ["crewneck-t-shirt", "v-neck-t-shirt", "long-sleeve-t-shirt"]
    assert "crew-sweatshirt" not in [p.slug for p in search("t-shirt")]  # "sweatshirt" is not a t-shirt
    assert [p.slug for p in search("tee")] == [p.slug for p in search("tshirt")] == [p.slug for p in search("T Shirt")]
    page = shop.get("/search", params={"q": "navy t-shirt"})
    assert "Showing 1–3 of 3 products" in _text(page)
    assert 'href="/p/crewneck-t-shirt?colour=navy"' in page.text and "/img/crewneck-t-shirt/navy.svg" in page.text
    assert "Nothing matches “zebra”" in _text(shop.get("/search", params={"q": "zebra"}))


def test_categories_filter_sort_and_page(shop):
    clothing = by_category("clothing")
    first = _text(shop.get("/c/clothing"))
    assert f"Showing 1–12 of {len(clothing)} products" in first and "Next page" in first
    second = _text(shop.get("/c/clothing", params={"page": 2}))
    assert f"Showing 13–{len(clothing)} of {len(clothing)} products" in second and "Previous page" in second

    cheap_first = shop.get("/c/home", params={"sort": "price-asc"}).text
    names = re.findall(r'class="card-name">([^<]+)<', cheap_first)
    prices = [PRODUCTS[slug].price for slug in re.findall(r'href="/p/([a-z0-9-]+)"', cheap_first)]
    assert prices == sorted(prices) and names[0] == "Cotton Tea Towels, Set of 2"

    filtered = _text(shop.get("/c/clothing", params=[("colour", "Mustard"), ("size", "XS"), ("price", "over-50"), ("in_stock", "1")]))
    assert "Showing 1–1 of 1 product" in filtered and "Rain Jacket" in filtered and "Clear filters" in filtered
    assert "No products found" in _text(shop.get("/c/accessories", params={"colour": "Blush"}))
    assert shop.get("/c/garden").status_code == 404


def test_a_product_needs_a_size_and_tells_you_what_was_added(shop):
    html = shop.get("/p/crewneck-t-shirt", params={"colour": "navy"}).text
    assert "Colour: Navy" in _text_of(html) and 'disabled aria-label="XS, sold out"' in html and "4.6 out of 5" in _text_of(html)
    missing = shop.post("/cart/add", data={"slug": "crewneck-t-shirt", "colour": "Navy", "quantity": 1})
    assert missing.status_code == 422 and "Choose a size." in _text(missing)
    sold_out = _buy(shop, size="XS")
    assert sold_out.status_code == 422 and "Size XS is sold out" in _text(sold_out)
    assert shop.post("/cart/add", data={"slug": "wool-scarf", "colour": "Burgundy"}).status_code == 422  # out of stock

    added = _buy(shop, quantity=2)
    assert added.url.path == "/p/crewneck-t-shirt"
    assert "Added to your cart: Crewneck T-Shirt, Navy, L, quantity 2." in _text(added) and "View cart (2)" in _text(added)


def test_the_cart_updates_removes_and_counts_toward_free_delivery(shop):
    _buy(shop)
    cart = _text(shop.get("/cart"))
    assert "Cart (1)" in cart and "Add $26.00 more for free delivery." in cart
    shop.post("/cart/update", data={"line": "l1", "quantity": 3})
    assert "Your order qualifies for free delivery." in _text(shop.get("/cart")) and "$72.00" in _text(shop.get("/cart"))
    removed = shop.post("/cart/remove", data={"line": "l1"})
    assert "Removed Crewneck T-Shirt from your cart." in _text(removed) and "Your cart is empty." in _text(removed)


def test_signing_in_and_the_account(shop):
    assert shop.get("/account").url.path == "/signin"
    wrong = shop.post("/signin", data={"email": "demo@larkspur.test", "password": "nope", "next": "/account"})
    assert wrong.status_code == 401 and "do not match" in _text(wrong)
    account = shop.post("/signin", data={"email": "demo@larkspur.test", "password": "larkspur-demo", "next": "/account"})
    text = _text(account)
    assert "Signed in as Alex Morgan" in text and "Order 1000" in text and "12 Orchard Lane" in text and "Hi, Alex" in text
    assert shop.post("/signin", data={"email": "x", "password": "y", "next": "https://elsewhere.example"}).status_code == 401
    assert shop.get("/signin", params={"next": "//elsewhere.example"}).url.path == "/account"  # never sent off-site

    bad = shop.post("/account/addresses", data={"name": "", "line1": "", "city": "", "postcode": "", "country": "Mars"})
    assert bad.status_code == 422 and "There are 5 problems" in _text(bad)
    saved = shop.post("/account/addresses", data=ADDRESS)
    assert "Address saved." in _text(saved) and "4 Elm Street" in _text(saved)
    shop.post("/account/addresses/remove", data={"index": 1})
    assert "4 Elm Street" not in _text(shop.get("/account"))
    assert "Sign in" in _text(shop.post("/signout"))


def test_checkout_checks_each_step_and_takes_only_test_cards(shop):
    assert shop.get("/checkout").url.path == "/cart"  # nothing to check out
    _buy(shop)
    assert shop.get("/checkout/payment").url.path == "/checkout/shipping"  # no skipping ahead

    blank = shop.post("/checkout/shipping", data={"address": "new", "delivery": "standard", "country": "United States"})
    assert blank.status_code == 422 and "There are 5 problems" in _text(blank) and "Enter an email address" in _text(blank)
    shop.post("/checkout/shipping", data={"email": "sam@example.com", "address": "new", "delivery": "express", **ADDRESS})

    def pay(number, expiry="12/39", cvc="123"):
        return shop.post("/checkout/payment", data={"card_name": "Sam Rivera", "card_number": number, "expiry": expiry, "cvc": cvc})

    assert "Enter the 16-digit card number." in _text(pay("4242"))
    assert "not valid" in _text(pay("4242 4242 4242 4241"))
    assert "takes only test cards" in _text(pay("5555 5555 5555 4444"))  # a real-looking card: refused
    assert "has expired" in _text(pay(CARD, expiry="01/20")) and "3-digit security code" in _text(pay(CARD, cvc="12"))

    review = pay(CARD)
    text = _text(review)
    assert review.url.path == "/checkout/review" and "Card ending 4242" in text and "Express delivery, Next working day" in text
    assert "Total $33.95" in text  # $24.00 and express delivery
    placed = shop.post("/checkout/place")
    assert placed.url.path == "/orders/1001" and "Thank you, your order is placed" in _text(placed) and "Order number 1001" in _text(placed)
    assert "Cart (0)" in _text(placed)


def test_a_declined_card_takes_no_order(shop):
    _buy(shop)
    declined = _checkout(shop, card=DECLINED)
    assert declined.status_code == 402 and "Your card was declined" in _text(declined)
    assert shop.get("/orders/1001").status_code == 404 and "Cart (1)" in _text(shop.get("/cart"))


def test_every_fresh_browser_meets_the_same_shop_and_reset_restores_it(shop):
    other = TestClient(shop.app, base_url="http://127.0.0.1:8400")
    for client in (shop, other):
        _buy(client)
        assert _checkout(client).url.path == "/orders/1001"  # each visitor's first order is 1001
    assert other.get("/orders/1001").status_code == 200

    shop.post("/signin", data={"email": "demo@larkspur.test", "password": "larkspur-demo", "next": "/"})
    _buy(shop)
    assert shop.post("/__reset").json() == {"reset": 2}
    assert "Cart (0)" in _text(shop.get("/cart")) and shop.get("/orders/1001").status_code == 404
    assert "Signed in as Alex Morgan" in _text(shop.get("/account"))  # still signed in
    _buy(shop)
    assert _checkout(shop, email="").url.path == "/orders/1001"  # numbering starts again


def test_pictures_and_missing_pages(shop):
    picture = shop.get("/img/crewneck-t-shirt/navy.svg")
    assert picture.headers["content-type"].startswith("image/svg+xml") and "#1f2a44" in picture.text
    assert shop.get("/img/crewneck-t-shirt/plaid.svg").status_code == 404
    missing = shop.get("/p/no-such-thing")
    assert missing.status_code == 404 and "Page not found" in _text(missing)


# --- In a real browser, and with demogen


@pytest.fixture
def shop_url():
    port = _free_port()
    server = uvicorn.Server(uvicorn.Config(create_shop(), host="127.0.0.1", port=port, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    while not server.started:
        time.sleep(0.05)
    yield f"http://127.0.0.1:{port}"
    server.should_exit = True
    thread.join(timeout=5)


def test_a_guest_buys_a_navy_t_shirt_in_a_real_browser(shop_url):
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        page = browser.new_page()
        errors: list[str] = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.goto(shop_url)
        page.get_by_role("searchbox", name="Search products").fill("navy t-shirt")
        page.get_by_role("button", name="Search").click()
        expect(page.get_by_role("status")).to_have_text("Showing 1–3 of 3 products")
        page.get_by_role("link", name="Crewneck T-Shirt").click()
        expect(page.get_by_role("img", name="Crewneck T-Shirt in Navy")).to_be_visible()
        page.get_by_role("radio", name="Forest").check()  # the picture follows the colour
        expect(page.get_by_role("img", name="Crewneck T-Shirt in Forest")).to_be_visible()
        expect(page.get_by_text("Colour: Forest")).to_be_visible()
        page.get_by_role("radio", name="Navy").check()
        expect(page.get_by_role("radio", name="XS, sold out")).to_be_disabled()
        page.get_by_role("radio", name="L", exact=True).check()
        page.get_by_role("button", name="Add to cart").click()
        expect(page.get_by_role("status")).to_contain_text("Added to your cart: Crewneck T-Shirt, Navy, L, quantity 1.")
        page.get_by_role("link", name="Check out").click()

        page.get_by_role("textbox", name="Email address").fill("sam@example.com")
        page.get_by_role("textbox", name="Full name").fill("Sam Rivera")
        page.get_by_role("textbox", name="Address line 1").fill("4 Elm Street")
        page.get_by_role("textbox", name="Town or city").fill("Springfield")
        page.get_by_role("textbox", name="Postcode or ZIP code").fill("62701")
        page.get_by_role("button", name="Continue to payment").click()
        page.get_by_role("textbox", name="Card number").fill(CARD)
        page.get_by_role("textbox", name="Expiry date (MM/YY)").fill("12/39")
        page.get_by_role("textbox", name="Security code").fill("123")
        page.get_by_role("button", name="Review your order").click()
        expect(page.get_by_text("Card ending 4242")).to_be_visible()
        page.get_by_role("button", name="Place order").click()
        expect(page.get_by_role("heading", name="Thank you, your order is placed")).to_be_visible()
        expect(page.get_by_text("Order number 1001")).to_be_visible()
        assert errors == []
        browser.close()


def test_demogen_records_a_purchase_on_the_shop(harness, shop_url, tmp_path):
    """The whole purchase as a demogen take, with every action pinned: the shop works with demogen's driver."""
    playbook = {
        "version": 1,
        "name": "Buying a t-shirt at Larkspur Market",
        "target": {"base_url": shop_url, "viewport": {"width": 1280, "height": 800, "device_scale_factor": 1}},
        "setup": {"seed": f"curl -fsS -X POST {shop_url}/__reset"},
        "steps": [
            {"id": "search", "do": "Search for a navy t-shirt", "action": [
                {"fill": {"role": "searchbox", "name": "Search products", "text": "navy t-shirt"}}, {"press": "Enter"}]},
            {"id": "open", "do": "Open the Crewneck T-Shirt", "action": [{"click": {"role": "link", "name": "Crewneck T-Shirt"}}]},
            {"id": "size", "do": "Choose size L", "action": [{"click": {"role": "radio", "name": "L"}}]},
            {"id": "add", "do": "Add it to the cart", "action": [{"click": {"role": "button", "name": "Add to cart"}}]},
            {"id": "checkout", "do": "Go to checkout and fill in the delivery details", "action": [
                {"click": {"role": "link", "name": "Check out"}},
                {"fill": {"role": "textbox", "name": "Email address", "text": "sam@example.com"}},
                {"fill": {"role": "textbox", "name": "Full name", "text": "Sam Rivera"}},
                {"fill": {"role": "textbox", "name": "Address line 1", "text": "4 Elm Street"}},
                {"fill": {"role": "textbox", "name": "Town or city", "text": "Springfield"}},
                {"fill": {"role": "textbox", "name": "Postcode or ZIP code", "text": "62701"}},
                {"click": {"role": "button", "name": "Continue to payment"}}]},
            {"id": "pay", "do": "Pay with the test card and place the order", "action": [
                {"fill": {"role": "textbox", "name": "Card number", "text": CARD}},
                {"fill": {"role": "textbox", "name": "Expiry date (MM/YY)", "text": "12/39"}},
                {"fill": {"role": "textbox", "name": "Security code", "text": "123"}},
                {"click": {"role": "button", "name": "Review your order"}},
                {"click": {"role": "button", "name": "Place order"}}]},
        ],
    }  # fmt: skip
    path = tmp_path / "larkspur.yaml"
    path.write_text(yaml.safe_dump(playbook, sort_keys=False))
    h = harness(real="browser")
    status = h.start(path, auto_approve=True)
    assert status.phase == "done"
    plan = h.json("r1", "actions.lock.json")
    assert all(step["source"] == "playbook" for step in plan["steps"])
    assert not h.path("r1", "failures").exists()  # nothing went wrong on the way
