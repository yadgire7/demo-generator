"""Larkspur Market: a small shop to make demos of, run on this machine by `demogen shop`.

Real page addresses, server-side carts and orders, a test account and test-card checkout, and
no network: a dependable stand-in for an online store, for demos and for testing demogen.
"""

from demogen.testshop.app import create_shop

DEFAULT_PORT = 8400

__all__ = ["DEFAULT_PORT", "create_shop"]
