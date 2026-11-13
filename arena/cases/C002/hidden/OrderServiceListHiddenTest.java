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
import java.util.Comparator;
import java.util.HashMap;
import java.util.List;
import java.util.Map;
import java.util.Optional;
import org.junit.jupiter.api.Test;

/**
 * Hidden grading test for C002. The bug passed page*size straight to the repository as a row
 * offset, so page=1 skipped the first `size` rows. The contract is that page is 1-based:
 * page=1 must contain the oldest orders. Page-2 coverage guards against overfitting
 * (e.g. hard-coding offset 0).
 */
class OrderServiceListHiddenTest {

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

        @Override
        public List<Order> findPage(int offset, int size) {
            if (offset < 0) {
                throw new IllegalArgumentException("negative offset " + offset);
            }
            return rows.values().stream()
                    .sorted(Comparator.comparing(Order::id))
                    .skip(offset)
                    .limit(size)
                    .toList();
        }
    }

    private final MemoryOrders orders = new MemoryOrders();
    private final OrderService service = new OrderService(
            sku -> Optional.of(new BigDecimal("10.00")),
            code -> Optional.empty(),
            new FakeInventory(), orders, new PriceCalculator(),
            Clock.fixed(Instant.parse("2026-01-01T00:00:00Z"), ZoneOffset.UTC), null);

    private long createOrder(String customer) {
        Order o = service.create(new OrderService.CreateOrder(customer,
                List.of(new OrderService.LineRequest("SKU-1", 1)), null));
        return o.id();
    }

    @Test
    void firstPageContainsTheOldestOrders() {
        long a = createOrder("a");
        long b = createOrder("b");
        long c = createOrder("c");

        List<Order> page1 = service.listPage(1, 2);
        assertThat(page1).extracting(Order::id).containsExactly(a, b);
    }

    @Test
    void secondPageReturnsTheRemainder() {
        long a = createOrder("a");
        long b = createOrder("b");
        long c = createOrder("c");

        List<Order> page2 = service.listPage(2, 2);
        assertThat(page2).extracting(Order::id).containsExactly(c);
    }

    @Test
    void pageBeyondEndIsEmptyNotAnError() {
        createOrder("a");
        assertThat(service.listPage(5, 10)).isEmpty();
    }
}
