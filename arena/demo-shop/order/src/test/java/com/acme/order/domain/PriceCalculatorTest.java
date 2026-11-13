package com.acme.order.domain;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;

import java.math.BigDecimal;
import java.util.List;
import org.junit.jupiter.api.Test;

class PriceCalculatorTest {

    private final PriceCalculator calc = new PriceCalculator();

    private static List<OrderLine> lines() {
        return List.of(new OrderLine("SKU-1", 1, new BigDecimal("299.00")), new OrderLine("SKU-2", 2, new BigDecimal("89.90")));
    }

    @Test
    void noCoupon() {
        PriceBreakdown p = calc.calculate(lines(), null);
        assertThat(p.subtotal()).isEqualByComparingTo("478.80");
        assertThat(p.discount()).isEqualByComparingTo("0");
        assertThat(p.total()).isEqualByComparingTo("478.80");
        assertThat(p.appliedCoupon()).isNull();
    }

    @Test
    void amountCoupon() {
        PriceBreakdown p = calc.calculate(lines(), new Coupon("SAVE20", Coupon.Type.AMOUNT, new BigDecimal("20"), new BigDecimal("100")));
        assertThat(p.total()).isEqualByComparingTo("458.80");
        assertThat(p.appliedCoupon()).isEqualTo("SAVE20");
    }

    @Test
    void percentCoupon() {
        PriceBreakdown p = calc.calculate(lines(), new Coupon("OFF10", Coupon.Type.PERCENT, new BigDecimal("10"), BigDecimal.ZERO));
        assertThat(p.discount()).isEqualByComparingTo("47.88");
        assertThat(p.total()).isEqualByComparingTo("430.92");
    }

    @Test
    void couponBelowThresholdIsIgnored() {
        List<OrderLine> small = List.of(new OrderLine("SKU-2", 1, new BigDecimal("89.90")));
        PriceBreakdown p = calc.calculate(small, new Coupon("SAVE20", Coupon.Type.AMOUNT, new BigDecimal("20"), new BigDecimal("100")));
        assertThat(p.total()).isEqualByComparingTo("89.90");
        assertThat(p.appliedCoupon()).isNull();
    }

    @Test
    void discountNeverExceedsSubtotal() {
        List<OrderLine> small = List.of(new OrderLine("SKU-2", 1, new BigDecimal("10.00")));
        PriceBreakdown p = calc.calculate(small, new Coupon("BIG", Coupon.Type.AMOUNT, new BigDecimal("50"), BigDecimal.ZERO));
        assertThat(p.total()).isEqualByComparingTo("0");
    }

    @Test
    void emptyOrderIsRejected() {
        assertThatThrownBy(() -> calc.calculate(List.of(), null)).isInstanceOf(BizException.class);
    }
}
