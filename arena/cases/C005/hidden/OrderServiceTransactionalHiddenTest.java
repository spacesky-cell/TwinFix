package com.acme.order.service;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;

import com.acme.order.domain.BizException;
import com.acme.order.domain.Order;
import com.acme.order.domain.PriceCalculator;
import com.acme.order.infra.InventoryGateway;
import com.acme.order.infra.OrderRepository;
import java.math.BigDecimal;
import java.time.Clock;
import java.time.Instant;
import java.time.ZoneOffset;
import java.util.HashMap;
import java.util.List;
import java.util.Map;
import java.util.Optional;
import org.junit.jupiter.api.Test;
import org.springframework.transaction.annotation.Transactional;

/**
 * Hidden grading test for C005. The bug was confirmPaid calling this.markPaid directly,
 * bypassing the Spring proxy so @Transactional never took effect. Verifies the fix routes
 * through the injected self reference (so a proxy can intercept), and that markPaid keeps
 * its transactional semantics and business behavior.
 */
class OrderServiceTransactionalHiddenTest {

    static class FakeInventory implements InventoryGateway {
        @Override
        public void reserve(String sku, int quantity) {
        }

        @Override
        public void release(String sku, int quantity) {
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
            Order o = rows.get(id);
            if (o != null) {
                rows.put(id, new Order(o.id(), o.customer(), o.lines(), o.price(), status, o.createdAt()));
            }
        }
    }

    /**
     * Simulates the Spring proxy: same public surface, intercepts markPaid so we can see
     * whether confirmPaid reached it. If confirmPaid used {@code this}, the spy stays at 0.
     */
    static class SpySelf extends OrderService {
        int markPaidCalls;
        private final OrderService delegate;

        SpySelf(OrderService delegate) {
            super(null, null, null, null, null, Clock.systemUTC(), null);
            this.delegate = delegate;
        }

        @Override
        public Order markPaid(long id) {
            markPaidCalls++;
            return delegate.markPaid(id);
        }
    }

    private OrderService serviceWithSelf(OrderService self, MemoryOrders orders) {
        return new OrderService(
                sku -> Optional.of(new BigDecimal("10.00")),
                code -> Optional.empty(),
                new FakeInventory(), orders, new PriceCalculator(),
                Clock.fixed(Instant.parse("2026-01-01T00:00:00Z"), ZoneOffset.UTC), self);
    }

    @Test
    void confirmPaidGoesThroughInjectedSelfReference() {
        MemoryOrders orders = new MemoryOrders();
        OrderService delegate = serviceWithSelf(null, orders);
        SpySelf spy = new SpySelf(delegate);
        OrderService service = serviceWithSelf(spy, orders);

        Order o = service.create(new OrderService.CreateOrder("u", List.of(new OrderService.LineRequest("SKU-1", 1)), null));
        Order paid = service.confirmPaid(o.id());

        assertThat(spy.markPaidCalls).as("confirmPaid must invoke markPaid via the injected self reference").isEqualTo(1);
        assertThat(paid.status()).isEqualTo("PAID");
        assertThat(service.get(o.id()).status()).isEqualTo("PAID");
    }

    @Test
    void markPaidRemainsTransactional() throws Exception {
        assertThat(OrderService.class.getMethod("markPaid", long.class).isAnnotationPresent(Transactional.class))
                .as("markPaid must stay @Transactional so a real proxy opens a tx")
                .isTrue();
    }

    @Test
    void confirmPaidOnMissingOrderIsBizError() {
        MemoryOrders orders = new MemoryOrders();
        OrderService service = serviceWithSelf(serviceWithSelf(null, orders), orders);
        assertThatThrownBy(() -> service.confirmPaid(999L)).isInstanceOf(BizException.class);
    }
}
