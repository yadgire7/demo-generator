"""Larkspur Market's catalog: fixed, so every visit shows the same shop."""

from __future__ import annotations

import re
from dataclasses import dataclass, field

COLOURS = {
    "Navy": "#1f2a44", "Heather Grey": "#9aa0a6", "White": "#f4f4f1", "Black": "#1d1d1f", "Forest": "#2f5d3a",
    "Rust": "#b5532d", "Sand": "#d8c3a0", "Sky": "#8ab6d6", "Charcoal": "#3c3f44", "Burgundy": "#6d2232",
    "Mustard": "#d4a017", "Cream": "#efe6d2", "Olive": "#6b6b35", "Teal": "#2a7f7f", "Blush": "#e8b4b8",
}  # fmt: skip

CATEGORIES = {
    "clothing": ("Clothing", "Everyday basics, made to last."),
    "home": ("Home & Kitchen", "Things for the kitchen, the sofa and the shelf."),
    "accessories": ("Accessories", "Bags, caps and the small things that go with you."),
}

APPAREL_SIZES = ["XS", "S", "M", "L", "XL"]
SIZE_NAMES = {"XS": "Extra small", "S": "Small", "M": "Medium", "L": "Large", "XL": "Extra large"}

REVIEWERS = ["Sam P.", "Jordan K.", "Priya N.", "Chris L.", "Morgan T.", "Ali R.", "Dana W.", "Ren S."]
REVIEW_LINES = [
    ("Exactly as described", "Good quality and the colour is just like the pictures."),
    ("Would buy again", "Arrived quickly and has held up well after a few weeks."),
    ("Nice, but check the sizing", "Runs a little small, so I would go one size up."),
    ("Great value", "Better than I expected for the price."),
    ("Solid everyday pick", "Not fancy, but it does the job every day."),
]


@dataclass(frozen=True)
class Product:
    slug: str
    name: str
    category: str
    kind: str  # what is drawn for its picture
    price: int  # cents
    colours: tuple[str, ...]
    description: str
    fit: str = "unisex"  # mens, womens or unisex: searchable
    sizes: tuple[str, ...] = ()
    sold_out: tuple[str, ...] = ()  # sizes with none left
    stock: int = 25  # how many are left; 0 is sold out, under 5 shows "Only N left"
    rating: float = 4.5
    review_count: int = 40
    featured: bool = False
    reviews: tuple[tuple[str, int, str, str], ...] = field(default=(), compare=False)

    @property
    def in_stock(self) -> bool:
        return self.stock > 0

    def size_available(self, size: str) -> bool:
        return size in self.sizes and size not in self.sold_out


def _p(slug, name, category, kind, dollars, colours, description, **extra) -> Product:
    index = len(_RAW)
    reviews = tuple(
        (REVIEWERS[(index + n) % len(REVIEWERS)], 5 - ((index + n) % 3 == 2), *REVIEW_LINES[(index + n) % len(REVIEW_LINES)])
        for n in range(3)
    )
    product = Product(slug, name, category, kind, round(dollars * 100), tuple(colours), description, reviews=reviews, **extra)
    _RAW.append(product)
    return product


_RAW: list[Product] = []
_ALL = APPAREL_SIZES
# Clothing: enough to need a second page.
_p("crewneck-t-shirt", "Crewneck T-Shirt", "clothing", "tshirt", 24, ["Heather Grey", "Navy", "White", "Black", "Forest"],
   "A soft cotton tee with a classic crew neck. Pre-washed, so it keeps its shape.", fit="mens", sizes=_ALL, sold_out=("XS",), rating=4.6, review_count=128, featured=True)
_p("v-neck-t-shirt", "V-Neck T-Shirt", "clothing", "tshirt", 22, ["White", "Navy", "Blush"],
   "A lightweight tee with a relaxed v-neck, cut for an easy fit.", fit="womens", sizes=_ALL, rating=4.4, review_count=76)
_p("pocket-t-shirt", "Pocket T-Shirt", "clothing", "tshirt", 26, ["Sand", "Olive", "Charcoal"],
   "A heavier cotton tee with a chest pocket.", fit="mens", sizes=_ALL, sold_out=("XL",), rating=4.5, review_count=54)
_p("long-sleeve-t-shirt", "Long Sleeve T-Shirt", "clothing", "tshirt", 30, ["Navy", "Heather Grey", "Rust"],
   "The crewneck tee, with long sleeves for cooler days.", fit="unisex", sizes=_ALL, rating=4.3, review_count=41)
_p("oxford-shirt", "Oxford Shirt", "clothing", "shirt", 48, ["White", "Sky"],
   "A button-down oxford shirt in breathable cotton. Smart enough for work.", fit="mens", sizes=_ALL, rating=4.7, review_count=93, featured=True)
_p("linen-shirt", "Linen Shirt", "clothing", "shirt", 55, ["Sand", "White", "Olive"],
   "A loose linen shirt that stays cool in the heat.", fit="womens", sizes=_ALL, sold_out=("M",), rating=4.2, review_count=37)
_p("flannel-shirt", "Flannel Shirt", "clothing", "shirt", 52, ["Burgundy", "Forest"],
   "A brushed flannel shirt, warm and soft from the first wear.", fit="mens", sizes=_ALL, rating=4.6, review_count=66)
_p("pullover-hoodie", "Pullover Hoodie", "clothing", "hoodie", 58, ["Heather Grey", "Navy", "Black"],
   "A midweight fleece hoodie with a kangaroo pocket.", fit="unisex", sizes=_ALL, rating=4.8, review_count=210, featured=True)
_p("zip-hoodie", "Zip Hoodie", "clothing", "hoodie", 64, ["Charcoal", "Forest"],
   "A full-zip hoodie with a lined hood.", fit="unisex", sizes=_ALL, sold_out=("XS", "S"), rating=4.5, review_count=88)
_p("crew-sweatshirt", "Crew Sweatshirt", "clothing", "hoodie", 46, ["Cream", "Navy", "Mustard"],
   "A classic crew-neck sweatshirt in loopback cotton.", fit="womens", sizes=_ALL, rating=4.4, review_count=59)
_p("rain-jacket", "Rain Jacket", "clothing", "jacket", 89, ["Mustard", "Navy", "Black"],
   "A packable waterproof jacket with taped seams.", fit="unisex", sizes=_ALL, stock=3, rating=4.6, review_count=47)
_p("quilted-vest", "Quilted Vest", "clothing", "jacket", 69, ["Olive", "Black"],
   "A light quilted vest for layering.", fit="mens", sizes=_ALL, rating=4.1, review_count=22)
_p("chino-shorts", "Chino Shorts", "clothing", "shorts", 38, ["Sand", "Navy", "Olive"],
   "Cotton chino shorts with a 7-inch inseam.", fit="mens", sizes=_ALL, rating=4.3, review_count=35)
_p("running-shorts", "Running Shorts", "clothing", "shorts", 32, ["Black", "Teal"],
   "Quick-dry shorts with a zip pocket for keys.", fit="womens", sizes=_ALL, rating=4.5, review_count=61)
_p("wool-socks", "Wool Socks, 3 Pairs", "clothing", "socks", 18, ["Charcoal", "Cream"],
   "Merino blend socks that stay warm and do not itch.", fit="unisex", sizes=("S", "M", "L"), rating=4.7, review_count=144)
_p("knit-beanie", "Knit Beanie", "clothing", "beanie", 20, ["Rust", "Navy", "Heather Grey", "Mustard"],
   "A ribbed beanie in soft recycled wool.", fit="unisex", rating=4.6, review_count=97)
_p("cotton-pyjamas", "Cotton Pyjama Set", "clothing", "shirt", 60, ["Sky", "Blush"],
   "A button-up pyjama shirt and trousers in brushed cotton.", fit="womens", sizes=_ALL, stock=0, rating=4.4, review_count=29)
# Home
_p("stoneware-mug", "Stoneware Mug", "home", "mug", 16, ["Cream", "Teal", "Charcoal"],
   "A hand-glazed 12oz mug. Dishwasher safe.", rating=4.8, review_count=182, featured=True)
_p("travel-mug", "Insulated Travel Mug", "home", "bottle", 28, ["Black", "Teal", "White"],
   "Keeps coffee hot for six hours, with a leak-proof lid.", rating=4.5, review_count=73)
_p("water-bottle", "Steel Water Bottle", "home", "bottle", 24, ["Forest", "Sky", "Black"],
   "A 750ml double-walled bottle that keeps water cold all day.", rating=4.6, review_count=120)
_p("wool-throw", "Wool Throw Blanket", "home", "throw", 85, ["Cream", "Rust", "Forest"],
   "A heavy woven throw in pure wool, 130 by 180cm.", stock=4, rating=4.9, review_count=58, featured=True)
_p("linen-cushion", "Linen Cushion Cover", "home", "cushion", 26, ["Sand", "Olive", "Blush"],
   "A washed linen cover for a 45cm cushion. Insert not included.", rating=4.3, review_count=44)
_p("soy-candle", "Soy Candle", "home", "candle", 22, ["Cream", "Charcoal"],
   "A hand-poured soy candle that burns for about 40 hours.", rating=4.4, review_count=91)
_p("tea-towels", "Cotton Tea Towels, Set of 2", "home", "towel", 14, ["White", "Navy"],
   "Absorbent waffle-weave towels for the kitchen.", rating=4.2, review_count=33)
# Accessories
_p("canvas-tote", "Canvas Tote Bag", "accessories", "bag", 22, ["Sand", "Black", "Navy"],
   "A sturdy canvas tote with an inside pocket.", rating=4.7, review_count=104, featured=True)
_p("everyday-backpack", "Everyday Backpack", "accessories", "backpack", 78, ["Black", "Olive", "Navy"],
   "A 20-litre backpack with a padded laptop sleeve.", rating=4.6, review_count=86)
_p("baseball-cap", "Baseball Cap", "accessories", "cap", 25, ["Navy", "Sand", "Black", "Forest"],
   "A washed cotton cap with an adjustable strap.", rating=4.5, review_count=69)
_p("leather-wallet", "Leather Wallet", "accessories", "wallet", 45, ["Black", "Rust"],
   "A slim leather wallet with six card slots.", rating=4.4, review_count=52)
_p("wool-scarf", "Wool Scarf", "accessories", "scarf", 38, ["Burgundy", "Charcoal", "Mustard"],
   "A long scarf in soft lambswool.", stock=0, rating=4.8, review_count=40)

PRODUCTS: dict[str, Product] = {product.slug: product for product in _RAW}


def by_category(category: str) -> list[Product]:
    return [product for product in _RAW if product.category == category]


def featured() -> list[Product]:
    return [product for product in _RAW if product.featured]


def _words(text: str) -> str:
    return re.sub(r"[^a-z0-9 ]", "", text.lower().replace("-", ""))


_FIT_WORDS = {"mens": "mens men man", "womens": "womens women woman", "unisex": "unisex mens men womens women"}


def search(query: str) -> list[Product]:
    """Products matching every word of `query`, in catalog order. 't-shirt', 'tshirt' and 't shirt' are the same."""
    query = _words(query).replace("t shirt", "tshirt")
    terms = ["tshirt" if term in ("tee", "tees") else term for term in query.split()]
    if not terms:
        return []
    found = []
    for product in _RAW:
        hay = " ".join([_words(product.name), product.kind, _words(" ".join(product.colours)), product.category, _FIT_WORDS[product.fit], _words(product.description)]).split()
        if all(_matches(term, hay) for term in terms):
            found.append(product)
    return found


def _matches(term: str, words: list[str]) -> bool:
    """A word that starts with the term, or with its singular: 'shirt' finds 'shirts', never 'sweatshirt'."""
    stems = {term, term[:-1]} if len(term) > 3 and term.endswith("s") else {term}
    return any(word.startswith(stem) for word in words for stem in stems)


def colour_in(query: str) -> str | None:
    """The colour a search names, if it names one: 'navy t-shirt' is Navy."""
    text = f" {_words(query)} "
    return next((colour for colour in COLOURS if f" {_words(colour)} " in text), None)


def colour_slug(colour: str) -> str:
    return colour.lower().replace(" ", "-")


def colour_from_slug(slug: str) -> str | None:
    return next((colour for colour in COLOURS if colour_slug(colour) == slug), None)
