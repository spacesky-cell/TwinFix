from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal


@dataclass(frozen=True)
class Coupon:
    code: str
    kind: str  # amount | percent
    value: Decimal
    threshold: Decimal = Decimal("0")


@dataclass(frozen=True)
class OrderLine:
    sku: str
    quantity: int
    unit_price: Decimal


@dataclass(frozen=True)
class PriceBreakdown:
    subtotal: Decimal
    discount: Decimal
    applied_coupon: str | None

    @property
    def total(self) -> Decimal:
        return self.subtotal - self.discount


@dataclass
class Order:
    id: str
    customer: str
    lines: list[OrderLine] = field(default_factory=list)
    total: Decimal = Decimal("0")
