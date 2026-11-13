package com.acme.order.domain;

import java.math.BigDecimal;

/** Result of pricing an order. {@code appliedCoupon} is null when no discount was given. */
public record PriceBreakdown(BigDecimal subtotal, BigDecimal discount, BigDecimal total, String appliedCoupon) {
}
