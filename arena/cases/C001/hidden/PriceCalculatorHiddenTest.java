package com.acme.order.domain;

import static org.assertj.core.api.Assertions.assertThat;

import java.math.BigDecimal;
import java.util.List;
import org.junit.jupiter.api.Test;

/** Hidden grading test for C001. Not visible to TwinFix; copied into the repo only when scoring. */
class PriceCalculatorHiddenTest {

    private final PriceCalculator calc = new PriceCalculator();
    private final Coupon save20 = new Coupon("SAVE20", Coupon.Type.AMOUNT, new BigDecimal("20"), new BigDecimal("100"));

    private static List<OrderLine> amount(String price) {
        return List.of(new OrderLine("SKU-X", 1, new BigDecimal(price)));
    }

    @Test
    void orderWithoutCouponIsPricedAtSubtotal() {
        PriceBreakdown p = calc.calculate(amount("179.80"), null);
        assertThat(p.total()).isEqualByComparingTo("179.80");
        assertThat(p.discount()).isEqualByComparingTo("0");
        assertThat(p.appliedCoupon()).isNull();
    }

    @Test
    void amountCouponAppliesAtConfiguredThreshold() {
        PriceBreakdown p = calc.calculate(amount("100.00"), save20);
        assertThat(p.total()).isEqualByComparingTo("80.00");
        assertThat(p.appliedCoupon()).isEqualTo("SAVE20");
    }

    @Test
    void discountNeverExceedsSubtotal() {
        Coupon big = new Coupon("BIG", Coupon.Type.AMOUNT, new BigDecimal("50"), BigDecimal.ZERO);
        assertThat(calc.calculate(amount("100.00"), big).total()).isEqualByComparingTo("50.00");
    }

    @Test
    void zeroThresholdAmountCouponStillGuarded() {
        // 门槛配成 0 的满减券，按"至少 2 倍面值"兜底：39.99 不可用，40 可用
        Coupon misconfigured = new Coupon("ZERO", Coupon.Type.AMOUNT, new BigDecimal("20"), BigDecimal.ZERO);
        assertThat(calc.calculate(amount("39.99"), misconfigured).appliedCoupon()).isNull();
        assertThat(calc.calculate(amount("40.00"), misconfigured).total()).isEqualByComparingTo("20.00");
    }

    @Test
    void amountCouponStillRejectedBelowThreshold() {
        PriceBreakdown p = calc.calculate(amount("99.99"), save20);
        assertThat(p.total()).isEqualByComparingTo("99.99");
        assertThat(p.appliedCoupon()).isNull();
    }

    @Test
    void percentCouponUnaffected() {
        Coupon off10 = new Coupon("OFF10", Coupon.Type.PERCENT, new BigDecimal("10"), BigDecimal.ZERO);
        assertThat(calc.calculate(amount("50.00"), off10).total()).isEqualByComparingTo("45.00");
    }
}
