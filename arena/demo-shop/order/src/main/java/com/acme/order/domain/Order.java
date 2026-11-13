package com.acme.order.domain;

import java.time.Instant;
import java.util.List;

public record Order(Long id, String customer, List<OrderLine> lines, PriceBreakdown price, String status, Instant createdAt) {

    public Order withId(long newId) {
        return new Order(newId, customer, lines, price, status, createdAt);
    }
}
