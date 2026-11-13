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

/** confirmPaid 经由 self 代理调用 markPaid，事务注解才会生效（防自调用回归）。 */
class OrderServicePayTest {

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

    private final MemoryOrders orders = new MemoryOrders();

    private OrderService service() {
        return new OrderService(
                sku -> Optional.of(new BigDecimal("10.00")),
                code -> Optional.empty(),
                new FakeInventory(), orders, new PriceCalculator(),
                Clock.fixed(Instant.parse("2026-01-01T00:00:00Z"), ZoneOffset.UTC), null);
    }

    @Test
    void confirmPaidMarksOrderPaid() {
        OrderService s = service();
        Order o = s.create(new OrderService.CreateOrder("u", List.of(new OrderService.LineRequest("SKU-1", 1)), null));
        Order paid = s.confirmPaid(o.id());
        assertThat(paid.status()).isEqualTo("PAID");
        assertThat(s.get(o.id()).status()).isEqualTo("PAID");
    }

    @Test
    void confirmPaidOnMissingOrderIsBizError() {
        assertThatThrownBy(() -> service().confirmPaid(999L)).isInstanceOf(BizException.class);
    }
}
