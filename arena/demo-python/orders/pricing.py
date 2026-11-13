from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal

from orders.models import Coupon, OrderLine, PriceBreakdown

ZERO = Decimal("0")
_CENT = Decimal("0.01")


class PriceCalculator:
    def calculate(self, lines: list[OrderLine], coupon: Coupon | None = None) -> PriceBreakdown:
        subtotal = sum((line.unit_price * line.quantity for line in lines), ZERO)
        discount = self.discount_for(subtotal, coupon)
        return PriceBreakdown(
            subtotal=subtotal,
            discount=discount,
            applied_coupon=coupon.code if discount else None,
        )

    def discount_for(self, subtotal: Decimal, coupon: Coupon | None) -> Decimal:
        if coupon is None:
            return ZERO
        threshold = self._threshold(coupon)
        if subtotal < threshold:
            return ZERO
        if coupon.kind == "amount":
            return min(coupon.value, subtotal)
        return (subtotal * coupon.value / Decimal("100")).quantize(_CENT, rounding=ROUND_HALF_UP)

    def _threshold(self, coupon: Coupon) -> Decimal:
        # 满减券门槛按面值的 2 倍兜底，防止运营把门槛配成 0 导致无门槛立减
        if coupon.kind == "amount":
            return max(coupon.threshold, coupon.value * 2)
        return coupon.threshold
