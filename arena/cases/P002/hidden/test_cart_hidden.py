"""Hidden grading test for P002. Not visible to TwinFix; copied into the repo only when scoring."""

from __future__ import annotations

from decimal import Decimal

from orders.cart import Cart
from orders.inventory import Inventory
from orders.models import OrderLine
from orders.service import OrderService


def _line(sku: str, price: str = "10.00", quantity: int = 1) -> OrderLine:
    return OrderLine(sku, quantity, Decimal(price))


def test_default_carts_do_not_share_lines() -> None:
    first = Cart("u-1")
    first.add(_line("SKU-1"))
    second = Cart("u-2")
    assert second.lines == []


def test_explicit_lines_are_not_mutated() -> None:
    source = [_line("SKU-1")]
    cart = Cart("u-1", source)
    cart.add(_line("SKU-2"))
    assert source == [_line("SKU-1")]
    assert len(cart.lines) == 2


def test_two_orders_are_priced_independently() -> None:
    inventory = Inventory({"SKU-1": 100, "SKU-2": 100})
    service = OrderService(inventory)
    first = service.create("u-1", [_line("SKU-1", "10.00")])
    second = service.create("u-2", [_line("SKU-2", "20.00")])
    assert first.total == Decimal("10.00")
    assert second.total == Decimal("20.00")


def test_coupon_applies_to_its_own_order_only() -> None:
    from orders.models import Coupon

    inventory = Inventory({"SKU-1": 100, "SKU-2": 100})
    service = OrderService(inventory)
    service.create("u-1", [_line("SKU-1", "100.00")])
    second = service.create(
        "u-2",
        [_line("SKU-2", "100.00")],
        Coupon("SAVE20", "amount", Decimal("20"), Decimal("100")),
    )
    assert second.total == Decimal("80.00")
