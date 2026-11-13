from __future__ import annotations

from decimal import Decimal

from orders.models import OrderLine


class Cart:
    def __init__(self, customer: str, lines: list[OrderLine] | None = None) -> None:
        self.customer = customer
        self.lines = list(lines) if lines is not None else []

    def add(self, line: OrderLine) -> None:
        self.lines.append(line)

    def subtotal(self) -> Decimal:
        return sum((line.unit_price * line.quantity for line in self.lines), Decimal("0"))
