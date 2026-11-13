package com.acme.order.service;

import static org.assertj.core.api.Assertions.assertThat;

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
import java.util.Objects;
import java.util.Optional;
import org.junit.jupiter.api.Test;

/**
 * Hidden grading test for C008. listForCustomer loaded each order with findById (N+1: two SQL per
 * order). Counts repository round-trips: the number of queries must not grow with the number of
 * orders, and results keep id order with full lines.
 */
class OrderServiceNPlusOneHiddenTest {

    static class CountingOrders implements OrderRepository {
        final Map<Long, Order> rows = new HashMap<>();
        int queries;

        @Override
        public Order save(Order order) {
            Order saved = order.withId(rows.size() + 1L);
            rows.put(saved.id(), saved);
            return saved;
        }

        @Override
        public Optional<Order> findById(long id) {
            queries += 2; // 订单 + 明细
            return Optional.ofNullable(rows.get(id));
        }

        @Override
        public void saveStatus(long id, String status) {
        }

        @Override
        public List<Long> findIdsByCustomer(String customer) {
            queries++;
            return rows.values().stream().filter(o -> o.customer().equals(customer)).map(Order::id).sorted().toList();
        }

        @Override
        public List<Order> findAllById(List<Long> ids) {
            queries += 2;
            return ids.stream().map(rows::get).filter(Objects::nonNull).toList();
        }
    }

    private final CountingOrders orders = new CountingOrders();
    private static final InventoryGateway NO_STOCK = new InventoryGateway() {
        @Override
        public void reserve(String sku, int quantity) {
        }

        @Override
        public void release(String sku, int quantity) {
        }
    };

    private final OrderService service = new OrderService(
            sku -> Optional.of(new BigDecimal("10.00")), code -> Optional.empty(), NO_STOCK, orders,
            new PriceCalculator(), Clock.fixed(Instant.parse("2026-01-01T00:00:00Z"), ZoneOffset.UTC), null);

    private void place(String customer, int n) {
        for (int i = 0; i < n; i++) {
            service.create(new OrderService.CreateOrder(customer, List.of(new OrderService.LineRequest("SKU-1", i + 1)), null));
        }
    }

    @Test
    void queryCountDoesNotGrowWithOrders() {
        place("few", 2);
        place("many", 50);
        orders.queries = 0;
        service.listForCustomer("few");
        int few = orders.queries;
        orders.queries = 0;
        service.listForCustomer("many");
        assertThat(orders.queries).as("queries for 50 orders vs 2 orders").isEqualTo(few).isLessThanOrEqualTo(4);
    }

    @Test
    void returnsOrdersInIdOrderWithLines() {
        place("eve", 3);
        place("other", 1);
        List<Order> list = service.listForCustomer("eve");
        assertThat(list).extracting(Order::customer).containsOnly("eve");
        assertThat(list).extracting(Order::id).isSorted().hasSize(3);
        assertThat(list.get(2).lines()).singleElement().satisfies(l -> assertThat(l.quantity()).isEqualTo(3));
    }

    @Test
    void unknownCustomerIsEmpty() {
        assertThat(service.listForCustomer("nobody")).isEmpty();
    }
}
