package com.acme.order.domain;

import java.math.BigDecimal;
import java.math.RoundingMode;

public record OrderLine(String sku, int quantity, BigDecimal unitPrice, BigDecimal discountRate) {

    public OrderLine(String sku, int quantity, BigDecimal unitPrice) {
        this(sku, quantity, unitPrice, BigDecimal.ZERO);
    }

    public OrderLine {
        if (quantity <= 0) {
            throw new BizException("INVALID_QUANTITY", "quantity must be positive: " + sku);
        }
        if (discountRate == null || discountRate.signum() < 0 || discountRate.compareTo(BigDecimal.ONE) > 0) {
            throw new BizException("INVALID_DISCOUNT", "discount rate out of range: " + sku);
        }
    }

    /** Line total after the per-line discount rate, rounded HALF_UP to cents. */
    public BigDecimal total() {
        return unitPrice
                .multiply(BigDecimal.valueOf(quantity))
                .multiply(BigDecimal.ONE.subtract(discountRate))
                .setScale(2, RoundingMode.HALF_UP);
    }
}
