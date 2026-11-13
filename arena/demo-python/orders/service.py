from __future__ import annotations

from decimal import Decimal

from orders.cart import Cart
from orders.inventory import Inventory
from orders.models import Coupon, Order, OrderLine
from orders.pricing import PriceCalculator


class OrderService:
    def __init__(self, inventory: Inventory, calculator: PriceCalculator | None = None) -> None:
        self.inventory = inventory
        self.calculator = calculator or PriceCalculator()
        self._orders: dict[str, Order] = {}
        self._seq = 0

    def create(self, customer: str, lines: list[OrderLine], coupon: Coupon | None = None) -> Order:
        if not lines:
            raise ValueError("order needs at least one line")
        cart = Cart(customer)
        try:
            for line in lines:
                self.inventory.reserve(line.sku, line.quantity)
                cart.add(line)
        except Exception:
            for line in cart.lines:
                self.inventory.release(line.sku, line.quantity)
            raise
        breakdown = self.calculator.calculate(cart.lines, coupon)
        self._seq += 1
        order = Order(
            id=f"o-{self._seq}",
            customer=customer,
            lines=list(cart.lines),
            total=Decimal(breakdown.total),
        )
        self._orders[order.id] = order
        return order

    def get(self, order_id: str) -> Order | None:
        return self._orders.get(order_id)
