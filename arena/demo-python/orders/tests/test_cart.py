from __future__ import annotations

from decimal import Decimal

from orders.cart import Cart
from orders.models import OrderLine


def _line(sku: str = "SKU-1", quantity: int = 1, price: str = "10.00") -> OrderLine:
    return OrderLine(sku, quantity, Decimal(price))


def test_explicit_lines_are_copied() -> None:
    source = [_line()]
    cart = Cart("u-1", source)
    cart.add(_line("SKU-2"))
    assert len(source) == 1
    assert len(cart.lines) == 2


def test_add_appends_to_empty_cart() -> None:
    cart = Cart("u-1", [])
    cart.add(_line())
    assert len(cart.lines) == 1
    assert cart.subtotal() == Decimal("10.00")


def test_subtotal_counts_quantities() -> None:
    cart = Cart("u-1", [_line(quantity=3)])
    assert cart.subtotal() == Decimal("30.00")
