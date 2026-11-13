package com.acme.order.domain;

import java.math.BigDecimal;
import java.math.RoundingMode;
import java.util.List;

/** Prices an order: subtotal of all lines, minus the coupon discount (never below zero). */
public class PriceCalculator {

    private static final BigDecimal HUNDRED = BigDecimal.valueOf(100);
    private static final BigDecimal ZERO = BigDecimal.ZERO.setScale(2);

    public PriceBreakdown calculate(List<OrderLine> lines, Coupon coupon) {
        if (lines == null || lines.isEmpty()) {
            throw new BizException("EMPTY_ORDER", "order has no lines");
        }
        BigDecimal subtotal = lines.stream()
                .map(OrderLine::total)
                .reduce(BigDecimal.ZERO, BigDecimal::add)
                .setScale(2, RoundingMode.HALF_UP);

        BigDecimal discount = discountFor(subtotal, coupon);
        BigDecimal total = subtotal.subtract(discount).max(BigDecimal.ZERO).setScale(2, RoundingMode.HALF_UP);
        String applied = discount.signum() > 0 ? coupon.code() : null;
        return new PriceBreakdown(subtotal, discount, total, applied);
    }

    private BigDecimal discountFor(BigDecimal subtotal, Coupon coupon) {
        // 无券或未达门槛：不打折
        if (coupon == null || !coupon.appliesTo(subtotal)) {
            return ZERO;
        }
        BigDecimal discount = switch (coupon.type()) {
            case AMOUNT -> coupon.value().min(subtotal);
            case PERCENT -> subtotal.multiply(coupon.value()).divide(HUNDRED, 2, RoundingMode.HALF_UP);
        };
        return discount.setScale(2, RoundingMode.HALF_UP);
    }
}
