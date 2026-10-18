"""Larkspur Market's web server: pages rendered on the server, plain forms, real addresses."""

from __future__ import annotations

import math
import re
from datetime import date
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.exceptions import HTTPException as StarletteHTTPException

from demogen.testshop.catalog import CATEGORIES, COLOURS, PRODUCTS, SIZE_NAMES, Product, by_category, colour_from_slug, colour_in, colour_slug, featured, search
from demogen.testshop.images import product_svg
from demogen.testshop.state import (
    ACCOUNTS, DELIVERY, EXPRESS_DELIVERY, FREE_DELIVERY_FROM, STANDARD_DELIVERY, TEST_CARDS, TEST_EMAIL, TEST_PASSWORD,
    Session, Sessions, money, order_total,
)  # fmt: skip

HERE = Path(__file__).parent
COOKIE = "larkspur_session"
PER_PAGE = 12
COUNTRIES = ["United States", "United Kingdom", "Canada", "Ireland", "Australia"]
PRICE_BANDS = {"any": ("Any price", 0, None), "under-25": ("Under $25", 0, 2500), "25-50": ("$25 to $50", 2500, 5000), "over-50": ("Over $50", 5000, None)}
SORTS = {"featured": "Featured", "price-asc": "Price: low to high", "price-desc": "Price: high to low", "rating": "Customer rating"}
ADDRESS_FIELDS = ("name", "line1", "line2", "city", "postcode", "country")


def create_shop() -> FastAPI:
    app = FastAPI(title="Larkspur Market", docs_url=None, redoc_url=None, openapi_url=None)
    sessions = Sessions()
    app.state.sessions = sessions
    templates = Jinja2Templates(directory=HERE / "templates")
    templates.env.globals.update(
        money=money, categories=CATEGORIES, colour_slug=colour_slug, colours=COLOURS, size_names=SIZE_NAMES,
        delivery=DELIVERY, order_total=order_total, test_email=TEST_EMAIL, test_password=TEST_PASSWORD,
    )  # fmt: skip
    app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")

    def visit(request: Request) -> Session:
        return sessions.get(request.cookies.get(COOKIE))

    def keep(response: Response, session: Session) -> Response:
        response.set_cookie(COOKIE, session.id, httponly=True, samesite="lax", path="/")
        return response

    def page(request: Request, session: Session, name: str, status_code: int = 200, **context: Any) -> Response:
        response = templates.TemplateResponse(request, name, {"session": session, "path": request.url.path, **context}, status_code=status_code)
        return keep(response, session)

    def go(session: Session, url: str) -> Response:
        return keep(RedirectResponse(url, status_code=303), session)

    @app.exception_handler(StarletteHTTPException)
    def not_found(request: Request, error: StarletteHTTPException) -> Response:
        if error.status_code != 404:
            return HTMLResponse(str(error.detail), status_code=error.status_code)
        return page(request, visit(request), "not_found.html", status_code=404)

    # --- Browsing

    @app.get("/")
    def home(request: Request) -> Response:
        return page(request, visit(request), "home.html", featured=featured())

    @app.get("/c/{category}")
    def category(request: Request, category: str) -> Response:
        if category not in CATEGORIES:
            raise StarletteHTTPException(404)
        title, blurb = CATEGORIES[category]
        return page(request, visit(request), "listing.html", title=title, blurb=blurb, category=category, **_listing(by_category(category), request))

    @app.get("/search")
    def search_page(request: Request, q: str = "") -> Response:
        found = search(q)
        return page(request, visit(request), "listing.html", title="Search", query=q.strip(), category=None, **_listing(found, request))

    @app.get("/p/{slug}")
    def product_page(request: Request, slug: str, colour: str | None = None, added: str | None = None) -> Response:
        product = PRODUCTS.get(slug) or _missing()
        session = visit(request)
        chosen = colour_from_slug(colour) if colour else None
        return page(
            request, session, "product.html", product=product, chosen_colour=chosen if chosen in product.colours else product.colours[0],
            chosen_size=None, quantity=1, errors={}, added=session.line(added) if added else None,
        )  # fmt: skip

    @app.get("/img/{slug}/{colour}.svg")
    def image(slug: str, colour: str) -> Response:
        product = PRODUCTS.get(slug) or _missing()
        name = colour_from_slug(colour) or _missing()
        return Response(product_svg(product.kind, name), media_type="image/svg+xml", headers={"Cache-Control": "public, max-age=3600"})

    # --- The cart

    @app.post("/cart/add")
    def add_to_cart(
        request: Request,
        slug: str = Form(...),
        colour: str = Form(""),
        size: str = Form(""),
        quantity: int = Form(1),
    ) -> Response:
        session = visit(request)
        product = PRODUCTS.get(slug) or _missing()
        errors = {}
        if colour not in product.colours:
            errors["colour"] = "Choose a colour."
        if product.sizes and not size:
            errors["size"] = "Choose a size."
        elif product.sizes and not product.size_available(size):
            errors["size"] = f"Size {size} is sold out. Choose another size."
        if not product.in_stock:
            errors["product"] = "This item is sold out."
        if not 1 <= quantity <= 5:
            errors["quantity"] = "Choose a quantity from 1 to 5."
        if errors:
            return page(
                request, session, "product.html", status_code=422, product=product, chosen_colour=colour if colour in product.colours else product.colours[0],
                chosen_size=size or None, quantity=quantity, errors=errors, added=None,
            )  # fmt: skip
        line = session.add(slug, colour, size or None, quantity)
        return go(session, f"/p/{slug}?{urlencode({'colour': colour_slug(colour), 'added': line.id})}")

    @app.get("/cart")
    def cart(request: Request, removed: str | None = None) -> Response:
        session = visit(request)
        return page(request, session, "cart.html", removed=removed, free_from=FREE_DELIVERY_FROM, standard=STANDARD_DELIVERY)

    @app.post("/cart/update")
    def update_line(request: Request, line: str = Form(...), quantity: int = Form(...)) -> Response:
        session = visit(request)
        found = session.line(line)
        if found:
            if quantity <= 0:
                session.cart.remove(found)
            else:
                found.quantity = min(quantity, 10)
        return go(session, "/cart")

    @app.post("/cart/remove")
    def remove_line(request: Request, line: str = Form(...)) -> Response:
        session = visit(request)
        found = session.line(line)
        if not found:
            return go(session, "/cart")
        session.cart.remove(found)
        return go(session, "/cart?" + urlencode({"removed": found.product.name}))

    # --- Signing in, and the account

    @app.get("/signin")
    def signin_page(request: Request, next: str = "/account") -> Response:
        session = visit(request)
        if session.user:
            return go(session, _safe(next))
        return page(request, session, "signin.html", next=_safe(next), email="", error=None)

    @app.post("/signin")
    def signin(request: Request, email: str = Form(""), password: str = Form(""), next: str = Form("/account")) -> Response:
        session = visit(request)
        account = ACCOUNTS.get(email.strip().lower())
        if not account or account["password"] != password:
            return page(request, session, "signin.html", status_code=401, next=_safe(next), email=email, error="That email address and password do not match. Check them and try again.")
        session.user = email.strip().lower()
        session.addresses = None
        return go(session, _safe(next))

    @app.post("/signout")
    def signout(request: Request) -> Response:
        session = visit(request)
        session.user, session.addresses = None, None
        return go(session, "/")

    @app.get("/account")
    def account(request: Request, saved: str | None = None) -> Response:
        session = visit(request)
        if not session.user:
            return go(session, "/signin?next=/account")
        return page(request, session, "account.html", saved=saved, errors={}, form={}, countries=COUNTRIES)

    @app.post("/account/addresses")
    async def add_address(request: Request) -> Response:
        session = visit(request)
        if not session.user:
            return go(session, "/signin?next=/account")
        form = {key: str(value).strip() for key, value in (await request.form()).items()}
        errors = _address_errors(form)
        if errors:
            return page(request, session, "account.html", status_code=422, saved=None, errors=errors, form=form, countries=COUNTRIES)
        session.saved_addresses().append({key: form.get(key, "") for key in ADDRESS_FIELDS})
        return go(session, "/account?saved=address")

    @app.post("/account/addresses/remove")
    def remove_address(request: Request, index: int = Form(...)) -> Response:
        session = visit(request)
        addresses = session.saved_addresses() if session.user else []
        if 0 <= index < len(addresses):
            addresses.pop(index)
        return go(session, "/account?saved=removed")

    @app.get("/orders/{number}")
    def order_page(request: Request, number: int, placed: int = 0) -> Response:
        session = visit(request)
        order = next((order for order in session.order_history() if order["number"] == number), None) or _missing()
        return page(request, session, "order.html", order=order, placed=bool(placed))

    # --- Checkout: shipping, payment, review

    @app.get("/checkout")
    def checkout(request: Request) -> Response:
        session = visit(request)
        return go(session, "/checkout/shipping" if session.cart else "/cart")

    @app.get("/checkout/shipping")
    def shipping_page(request: Request) -> Response:
        session = visit(request)
        if not session.cart:
            return go(session, "/cart")
        saved = session.saved_addresses() if session.user else []
        form = dict(session.checkout.get("form", {}))
        form.setdefault("address", "saved-0" if saved else "new")
        form.setdefault("delivery", "standard")
        form.setdefault("country", "United States")
        return page(request, session, "shipping.html", form=form, errors={}, saved=saved, countries=COUNTRIES, step=1, **_costs(session))

    @app.post("/checkout/shipping")
    async def shipping(request: Request) -> Response:
        session = visit(request)
        if not session.cart:
            return go(session, "/cart")
        form = {key: str(value).strip() for key, value in (await request.form()).items()}
        saved = session.saved_addresses() if session.user else []
        errors: dict[str, str] = {}
        email = session.user or form.get("email", "")
        if not session.user and not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email):
            errors["email"] = "Enter an email address, like name@example.com."
        choice = form.get("address", "new")
        if choice.startswith("saved-") and choice[6:].isdigit() and int(choice[6:]) < len(saved):
            address = saved[int(choice[6:])]
        else:
            errors.update(_address_errors(form))
            address = {key: form.get(key, "") for key in ADDRESS_FIELDS}
        if form.get("delivery") not in DELIVERY:
            errors["delivery"] = "Choose how you would like it delivered."
        if errors:
            return page(request, session, "shipping.html", status_code=422, form=form, errors=errors, saved=saved, countries=COUNTRIES, step=1, **_costs(session))
        session.checkout.update(form=form, email=email, address=address, delivery=form["delivery"])
        return go(session, "/checkout/payment")

    @app.get("/checkout/payment")
    def payment_page(request: Request) -> Response:
        session = visit(request)
        if "address" not in session.checkout:
            return go(session, "/checkout/shipping")
        form = {"card_name": session.checkout.get("card_name") or session.checkout["address"]["name"]}
        return page(request, session, "payment.html", form=form, errors={}, step=2, **_costs(session))

    @app.post("/checkout/payment")
    def payment(request: Request, card_name: str = Form(""), card_number: str = Form(""), expiry: str = Form(""), cvc: str = Form("")) -> Response:
        session = visit(request)
        if "address" not in session.checkout:
            return go(session, "/checkout/shipping")
        errors = _card_errors(card_name, card_number, expiry, cvc)
        if errors:
            form = {"card_name": card_name, "card_number": card_number, "expiry": expiry}
            return page(request, session, "payment.html", status_code=422, form=form, errors=errors, step=2, **_costs(session))
        digits = re.sub(r"[\s-]", "", card_number)
        session.checkout.update(card_name=card_name.strip(), card_last4=digits[-4:], card_result=TEST_CARDS[digits])
        return go(session, "/checkout/review")

    @app.get("/checkout/review")
    def review_page(request: Request) -> Response:
        session = visit(request)
        if "card_last4" not in session.checkout:
            return go(session, "/checkout/payment")
        return page(request, session, "review.html", error=None, step=3, **_costs(session))

    @app.post("/checkout/place")
    def place(request: Request) -> Response:
        session = visit(request)
        if not session.cart:
            return go(session, "/cart")
        if "card_last4" not in session.checkout:
            return go(session, "/checkout/payment")
        if session.checkout["card_result"] == "declined":
            error = "Your card was declined. No payment was taken. Use a different card to try again."
            return page(request, session, "review.html", status_code=402, error=error, step=3, **_costs(session))
        order = session.place_order()
        return go(session, f"/orders/{order['number']}?placed=1")

    # --- For tests and demogen's seed command

    @app.post("/__reset")
    def reset() -> JSONResponse:
        """Every visitor back to an empty cart and no new orders, still signed in."""
        return JSONResponse({"reset": sessions.reset()})

    @app.get("/__health")
    def health() -> JSONResponse:
        return JSONResponse({"ok": True})

    return app


def _missing() -> Any:
    raise StarletteHTTPException(404)


def _safe(target: str) -> str:
    """Only addresses on this site: a sign-in must not send anyone elsewhere."""
    return target if target.startswith("/") and not target.startswith("//") else "/account"


def _costs(session: Session) -> dict[str, Any]:
    option = session.checkout.get("delivery", "standard")
    delivery_cost = session.delivery_cost(option)
    return {
        "subtotal": session.subtotal(), "delivery_option": option, "delivery_cost": delivery_cost, "total": session.subtotal() + delivery_cost,
        "standard_cost": session.delivery_cost("standard"), "express_cost": EXPRESS_DELIVERY,
    }  # fmt: skip


def _address_errors(form: dict[str, str]) -> dict[str, str]:
    messages = {"name": "Enter the full name for delivery.", "line1": "Enter the first line of the address.", "city": "Enter a town or city.", "postcode": "Enter a postcode or ZIP code."}
    errors = {key: message for key, message in messages.items() if not form.get(key)}
    if form.get("country") not in COUNTRIES:
        errors["country"] = "Choose a country."
    return errors


def _luhn(digits: str) -> bool:
    total = 0
    for index, digit in enumerate(reversed(digits)):
        value = int(digit) * (2 if index % 2 else 1)
        total += value - 9 if value > 9 else value
    return total % 10 == 0


def _card_errors(name: str, number: str, expiry: str, cvc: str) -> dict[str, str]:
    errors = {}
    if not name.strip():
        errors["card_name"] = "Enter the name on the card."
    digits = re.sub(r"[\s-]", "", number)
    if not digits.isdigit() or len(digits) != 16:
        errors["card_number"] = "Enter the 16-digit card number."
    elif not _luhn(digits):
        errors["card_number"] = "That card number is not valid. Check it and try again."
    elif digits not in TEST_CARDS:
        errors["card_number"] = "This is a test shop and takes only test cards. Use 4242 4242 4242 4242."
    match = re.fullmatch(r"\s*(\d{1,2})\s*/\s*(\d{2})\s*", expiry)
    if not match or not 1 <= int(match.group(1)) <= 12:
        errors["expiry"] = "Enter the expiry date as MM/YY, like 08/29."
    else:
        today = date.today()
        if (2000 + int(match.group(2)), int(match.group(1))) < (today.year, today.month):
            errors["expiry"] = "This card has expired. Use a card with a future expiry date."
    if not re.fullmatch(r"\s*\d{3}\s*", cvc):
        errors["cvc"] = "Enter the 3-digit security code from the back of the card."
    return errors


def _listing(products: list[Product], request: Request) -> dict[str, Any]:
    """Filter, sort and page a list of products by the request's query string."""
    params = request.query_params
    colours = [c for c in params.getlist("colour") if c in COLOURS]
    sizes = [s for s in params.getlist("size") if s in SIZE_NAMES]
    price = params.get("price", "any") if params.get("price") in PRICE_BANDS else "any"
    in_stock = params.get("in_stock") == "1"
    sort = params.get("sort") if params.get("sort") in SORTS else "featured"
    _, low, high = PRICE_BANDS[price]
    shown = [
        p for p in products
        if (not colours or set(colours) & set(p.colours))
        and (not sizes or any(p.size_available(s) for s in sizes))
        and p.price >= low and (high is None or p.price < high)
        and (not in_stock or p.in_stock)
    ]  # fmt: skip
    if sort == "price-asc":
        shown.sort(key=lambda p: p.price)
    elif sort == "price-desc":
        shown.sort(key=lambda p: -p.price)
    elif sort == "rating":
        shown.sort(key=lambda p: (-p.rating, -p.review_count))
    pages = max(1, math.ceil(len(shown) / PER_PAGE))
    try:
        number = min(max(int(params.get("page", "1")), 1), pages)
    except ValueError:
        number = 1

    def link(**changes: Any) -> str:
        pairs = [(key, value) for key, value in params.multi_items() if key not in changes and key != "page"]
        pairs += [(key, value) for key, value in changes.items() if value is not None]
        return f"{request.url.path}?{urlencode(pairs)}" if pairs else request.url.path

    first = (number - 1) * PER_PAGE
    return {
        "products": shown[first : first + PER_PAGE],
        "total": len(shown),
        "first": first + 1 if shown else 0,
        "last": min(first + PER_PAGE, len(shown)),
        "page": number,
        "pages": pages,
        "page_link": lambda n: link(page=n if n > 1 else None),
        "facet_colours": [c for c in COLOURS if any(c in p.colours for p in products)],
        "facet_sizes": [s for s in SIZE_NAMES if any(s in p.sizes for p in products)],
        "chosen": {"colours": colours, "sizes": sizes, "price": price, "in_stock": in_stock, "sort": sort},
        "price_bands": PRICE_BANDS,
        "sorts": SORTS,
        "filtered": bool(colours or sizes or price != "any" or in_stock),
        "clear_link": _clear(request),
        # The colour cards show: the one filtered on, or the one searched for.
        "highlight": colours[0] if colours else colour_in(params.get("q", "")),
    }


def _clear(request: Request) -> str:
    keep = [(key, value) for key, value in request.query_params.multi_items() if key in ("q", "sort")]
    return f"{request.url.path}?{urlencode(keep)}" if keep else request.url.path
