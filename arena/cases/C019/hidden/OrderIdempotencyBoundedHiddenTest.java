package com.acme.order.service;

import static org.assertj.core.api.Assertions.assertThat;

import com.acme.order.domain.Order;
import com.acme.order.domain.PriceCalculator;
import com.acme.order.infra.InventoryGateway;
import com.acme.order.infra.OrderRepository;
import java.math.BigDecimal;
import java.time.Clock;
import java.time.Duration;
import java.time.Instant;
import java.time.ZoneId;
import java.time.ZoneOffset;
import java.util.HashMap;
import java.util.List;
import java.util.Map;
import java.util.Optional;
import org.junit.jupiter.api.Test;

/**
 * Hidden grading test for C019. The in-process idempotency table (Idempotency-Key → order id) was
 * only ever added to, so a long-running instance grew without bound until OOM. The table must stay
 * bounded (size cap and/or expiry after the 24h window) while recent keys still de-duplicate.
 */
class OrderIdempotencyBoundedHiddenTest {

    static class MutableClock extends Clock {
        Instant now = Instant.parse("2026-01-01T00:00:00Z");

        @Override
        public ZoneId getZone() {
            return ZoneOffset.UTC;
        }

        @Override
        public Clock withZone(ZoneId zone) {
            return this;
        }

        @Override
        public Instant instant() {
            return now;
        }
    }

    static class MemoryOrders implements OrderRepository {
        final Map<Long, Order> rows = new HashMap<>();

        @Override
        public Order save(Order order) {
            Order saved = order.withId(rows.size() + 1L);
            rows.put(saved.id(), saved);
            return saved;
        }

        @Override
        public Optional<Order> findById(long id) {
            return Optional.ofNullable(rows.get(id));
        }

        @Override
        public void saveStatus(long id, String status) {
        }
    }

    private final MutableClock clock = new MutableClock();
    private final MemoryOrders orders = new MemoryOrders();
    private final OrderService service = new OrderService(
            sku -> Optional.of(new BigDecimal("10.00")), code -> Optional.empty(), new InventoryGateway() {
                @Override
                public void reserve(String sku, int quantity) {
                }

                @Override
                public void release(String sku, int quantity) {
                }
            }, orders,
            new PriceCalculator(), clock, null);
    private final OrderService.CreateOrder req =
            new OrderService.CreateOrder("u", List.of(new OrderService.LineRequest("SKU-1", 1)), null);

    @Test
    void tableStaysBoundedUnderSustainedTraffic() {
        int total = 300_000;
        for (int i = 0; i < total; i++) {
            service.create(req, "key-" + i);
            clock.now = clock.now.plus(Duration.ofSeconds(1)); // ~3.5 days of traffic
        }
        assertThat(service.idempotencyEntries()).isLessThanOrEqualTo(total / 2);
    }

    @Test
    void recentKeysStillDeduplicate() {
        Order first = service.create(req, "retry-me");
        for (int i = 0; i < 50; i++) {
            service.create(req, "other-" + i);
            clock.now = clock.now.plus(Duration.ofMinutes(1));
        }
        Order replay = service.create(req, "retry-me");
        assertThat(replay.id()).isEqualTo(first.id());
        assertThat(orders.rows).hasSize(51);
    }
}
