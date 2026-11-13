package com.acme.order.domain;

import java.math.BigDecimal;

/**
 * A coupon either takes a fixed amount off ({@code AMOUNT}) or a percentage ({@code PERCENT}),
 * and only applies when the subtotal reaches {@code threshold}.
 */
public record Coupon(String code, Type type, BigDecimal value, BigDecimal threshold) {

    public enum Type { AMOUNT, PERCENT }

    public boolean appliesTo(BigDecimal subtotal) {
        return subtotal.compareTo(threshold) >= 0;
    }
}
