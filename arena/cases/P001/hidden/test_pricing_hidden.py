"""Hidden grading test for P001. Not visible to TwinFix; copied into the repo only when scoring."""

from __future__ import annotations

from decimal import Decimal

from orders.models import Coupon, OrderLine
from orders.pricing import PriceCalculator

CALC = PriceCalculator()


def _lines(price: str, quantity: int = 1) -> list[OrderLine]:
    return [OrderLine("SKU-X", quantity, Decimal(price))]


def test_order_without_coupon_is_priced_at_subtotal() -> None:
    breakdown = CALC.calculate(_lines("179.80"))
    assert breakdown.total == Decimal("179.80")
    assert breakdown.discount == Decimal("0")
    assert breakdown.applied_coupon is None


def test_amount_coupon_applies_at_configured_threshold() -> None:
    coupon = Coupon("SAVE20", "amount", Decimal("20"), Decimal("100"))
    breakdown = CALC.calculate(_lines("100.00"), coupon)
    assert breakdown.total == Decimal("80.00")
    assert breakdown.applied_coupon == "SAVE20"


def test_amount_coupon_rejected_below_threshold() -> None:
    coupon = Coupon("SAVE20", "amount", Decimal("20"), Decimal("100"))
    breakdown = CALC.calculate(_lines("99.99"), coupon)
    assert breakdown.total == Decimal("99.99")
    assert breakdown.applied_coupon is None


def test_zero_threshold_amount_coupon_still_guarded() -> None:
    # 门槛配成 0 的满减券按"至少 2 倍面值"兜底：39.99 不可用，40 可用
    coupon = Coupon("ZERO", "amount", Decimal("20"), Decimal("0"))
    assert CALC.calculate(_lines("39.99"), coupon).applied_coupon is None
    assert CALC.calculate(_lines("40.00"), coupon).total == Decimal("20.00")


def test_percent_coupon_unaffected() -> None:
    coupon = Coupon("OFF10", "percent", Decimal("10"))
    breakdown = CALC.calculate(_lines("50.00"), coupon)
    assert breakdown.total == Decimal("45.00")
    assert breakdown.applied_coupon == "OFF10"
