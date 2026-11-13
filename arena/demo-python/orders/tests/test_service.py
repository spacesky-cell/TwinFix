from __future__ import annotations

from decimal import Decimal

import pytest

from orders.inventory import Inventory, OutOfStock
from orders.models import OrderLine
from orders.service import OrderService


def _line(sku: str, quantity: int = 1, price: str = "10.00") -> OrderLine:
    return OrderLine(sku, quantity, Decimal(price))


def test_out_of_stock_is_a_business_rejection() -> None:
    inventory = Inventory({"SKU-3": 0})
    service = OrderService(inventory)
    with pytest.raises(OutOfStock):
        service.create("u-1", [_line("SKU-3")])
    assert inventory.available("SKU-3") == 0


def test_zero_quantity_is_rejected() -> None:
    inventory = Inventory({"SKU-1": 5})
    service = OrderService(inventory)
    with pytest.raises(ValueError):
        service.create("u-1", [_line("SKU-1", 0)])
    assert inventory.available("SKU-1") == 5


def test_failed_multi_line_order_releases_reserved_lines() -> None:
    inventory = Inventory({"SKU-1": 5, "SKU-3": 0})
    service = OrderService(inventory)
    with pytest.raises(OutOfStock):
        service.create("u-1", [_line("SKU-1", 2), _line("SKU-3", 1)])
    assert inventory.available("SKU-1") == 5
