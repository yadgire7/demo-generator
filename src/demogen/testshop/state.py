"""What Larkspur Market remembers: carts, sign-ins and orders, all in memory.

Carts, checkouts and the orders placed in a browser belong to that browser's session, so a fresh
browser (which is what demogen opens for each rehearsal and take) always meets the same shop:
an empty cart, and its first order numbered 1001. `reset` puts every session back that way while
keeping sign-ins, for playbooks that sign in once in setup and reuse that browser's cookies.
"""

from __future__ import annotations

import copy
import secrets
import threading
from dataclasses import dataclass, field
from datetime import date
from typing import Any

from demogen.testshop.catalog import PRODUCTS, Product

FIRST_ORDER = 1001
FREE_DELIVERY_FROM = 5000  # cents
STANDARD_DELIVERY = 495
EXPRESS_DELIVERY = 995
DELIVERY = {
    "standard": ("Standard delivery", "3 to 5 working days"),
    "express": ("Express delivery", "Next working day"),
}

# The test account: published in the README and on the sign-in page, since nothing here is real.
TEST_EMAIL = "demo@larkspur.test"
TEST_PASSWORD = "larkspur-demo"
ACCOUNTS: dict[str, dict[str, Any]] = {
    TEST_EMAIL: {
        "name": "Alex Morgan",
        "password": TEST_PASSWORD,
        "addresses": [
            {"name": "Alex Morgan", "line1": "12 Orchard Lane", "line2": "", "city": "Portland", "postcode": "97205", "country": "United States"},
        ],
        # A past order, so the account's order history is never empty.
        "orders": [
            {
                "number": 1000, "date": "12 September 2026", "status": "Delivered", "email": TEST_EMAIL,
                "lines": [{"slug": "stoneware-mug", "name": "Stoneware Mug", "colour": "Teal", "size": None, "quantity": 2, "price": 1600}],
                "address": {"name": "Alex Morgan", "line1": "12 Orchard Lane", "line2": "", "city": "Portland", "postcode": "97205", "country": "United States"},
                "delivery": "standard", "delivery_cost": STANDARD_DELIVERY, "card_last4": "4242",
            }
        ],
    }
}  # fmt: skip

# Published test cards, as payment providers publish them. No other card is accepted.
TEST_CARDS = {"4242424242424242": "ok", "4000000000000002": "declined"}


def money(cents: int) -> str:
    return f"${cents / 100:,.2f}"


@dataclass
class Line:
    id: str
    slug: str
    colour: str
    size: str | None
    quantity: int

    @property
    def product(self) -> Product:
        return PRODUCTS[self.slug]

    @property
    def total(self) -> int:
        return self.product.price * self.quantity

    def describe(self) -> str:
        parts = [self.product.name, self.colour] + ([self.size] if self.size else [])
        return ", ".join(parts)


@dataclass
class Session:
    id: str
    user: str | None = None  # the signed-in account's email
    cart: list[Line] = field(default_factory=list)
    checkout: dict[str, Any] = field(default_factory=dict)  # shipping and payment, as entered so far
    orders: list[dict[str, Any]] = field(default_factory=list)  # placed in this session
    addresses: list[dict[str, Any]] | None = None  # the account's addresses, as changed in this session
    next_order: int = FIRST_ORDER
    line_count: int = 0

    def subtotal(self) -> int:
        return sum(line.total for line in self.cart)

    def items(self) -> int:
        return sum(line.quantity for line in self.cart)

    def delivery_cost(self, option: str) -> int:
        if option == "express":
            return EXPRESS_DELIVERY
        return 0 if self.subtotal() >= FREE_DELIVERY_FROM else STANDARD_DELIVERY

    def add(self, slug: str, colour: str, size: str | None, quantity: int) -> Line:
        for line in self.cart:
            if (line.slug, line.colour, line.size) == (slug, colour, size):
                line.quantity = min(line.quantity + quantity, 10)
                return line
        self.line_count += 1
        line = Line(f"l{self.line_count}", slug, colour, size, quantity)
        self.cart.append(line)
        return line

    def line(self, line_id: str) -> Line | None:
        return next((line for line in self.cart if line.id == line_id), None)

    @property
    def account(self) -> dict[str, Any] | None:
        return ACCOUNTS.get(self.user) if self.user else None

    def saved_addresses(self) -> list[dict[str, Any]]:
        if self.addresses is None:
            self.addresses = copy.deepcopy(self.account["addresses"]) if self.account else []
        return self.addresses

    def order_history(self) -> list[dict[str, Any]]:
        """Orders this visitor can see, newest first: the account's past orders and this session's."""
        past = copy.deepcopy(self.account["orders"]) if self.account else []
        return sorted(self.orders + past, key=lambda order: order["number"], reverse=True)

    def place_order(self) -> dict[str, Any]:
        option = self.checkout["delivery"]
        order = {
            "number": self.next_order,
            "date": f"{date.today().day} {date.today():%B %Y}",
            "status": "Processing",
            "email": self.checkout["email"],
            "lines": [{"slug": l.slug, "name": l.product.name, "colour": l.colour, "size": l.size, "quantity": l.quantity, "price": l.product.price} for l in self.cart],
            "address": self.checkout["address"],
            "delivery": option,
            "delivery_cost": self.delivery_cost(option),
            "card_last4": self.checkout["card_last4"],
        }
        self.next_order += 1
        self.orders.append(order)
        self.cart = []
        self.checkout = {}
        return order

    def reset(self) -> None:
        """Back to a fresh visit, still signed in."""
        user = self.user
        self.__init__(self.id)  # type: ignore[misc]
        self.user = user


def order_total(order: dict[str, Any]) -> int:
    return sum(line["price"] * line["quantity"] for line in order["lines"]) + order["delivery_cost"]


class Sessions:
    """Every visitor's session, by the id in their cookie."""

    def __init__(self) -> None:
        self._sessions: dict[str, Session] = {}
        self._lock = threading.Lock()

    def get(self, session_id: str | None) -> Session:
        with self._lock:
            if session_id and session_id in self._sessions:
                return self._sessions[session_id]
            session = Session(secrets.token_urlsafe(16))
            self._sessions[session.id] = session
            return session

    def reset(self) -> int:
        with self._lock:
            for session in self._sessions.values():
                session.reset()
            return len(self._sessions)
