from __future__ import annotations

from decimal import Decimal

from orders.models import Coupon, OrderLine
from orders.pricing import PriceCalculator

CALC = PriceCalculator()


def _lines(price: str, quantity: int = 1) -> list[OrderLine]:
    return [OrderLine("SKU-X", quantity, Decimal(price))]


def test_no_coupon_uses_subtotal() -> None:
    breakdown = CALC.calculate(_lines("179.80"))
    assert breakdown.total == Decimal("179.80")
    assert breakdown.discount == Decimal("0")
    assert breakdown.applied_coupon is None


def test_amount_coupon_applies_at_threshold() -> None:
    coupon = Coupon("SAVE20", "amount", Decimal("20"), Decimal("100"))
    breakdown = CALC.calculate(_lines("100.00"), coupon)
    assert breakdown.total == Decimal("80.00")
    assert breakdown.applied_coupon == "SAVE20"


def test_amount_coupon_rejected_below_threshold() -> None:
    coupon = Coupon("SAVE20", "amount", Decimal("20"), Decimal("100"))
    breakdown = CALC.calculate(_lines("99.99"), coupon)
    assert breakdown.total == Decimal("99.99")
    assert breakdown.applied_coupon is None


def test_percent_coupon_rounds_half_up() -> None:
    coupon = Coupon("OFF10", "percent", Decimal("10"))
    breakdown = CALC.calculate(_lines("50.00"), coupon)
    assert breakdown.total == Decimal("45.00")


def test_amount_coupon_threshold_scales_with_face_value() -> None:
    # 满减券门槛至少是面值的 2 倍：30 元订单不能直接用 50 元券，100 元订单可以
    coupon = Coupon("BIG", "amount", Decimal("50"), Decimal("0"))
    assert CALC.calculate(_lines("30.00"), coupon).total == Decimal("30.00")
    assert CALC.calculate(_lines("100.00"), coupon).total == Decimal("50.00")
