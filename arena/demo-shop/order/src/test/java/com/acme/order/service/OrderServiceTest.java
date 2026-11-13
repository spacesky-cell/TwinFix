package com.acme.order.service;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;

import com.acme.order.domain.BizException;
import com.acme.order.domain.Coupon;
import com.acme.order.domain.Order;
import com.acme.order.domain.PriceCalculator;
import com.acme.order.infra.InventoryGateway;
import com.acme.order.infra.OrderRepository;
import java.math.BigDecimal;
import java.time.Clock;
import java.time.Instant;
import java.time.ZoneOffset;
import java.util.ArrayList;
import java.util.HashMap;
import java.util.List;
import java.util.Map;
import java.util.Optional;
import org.junit.jupiter.api.Test;

class OrderServiceTest {

    static class FakeInventory implements InventoryGateway {
        final Map<String, Integer> stock = new HashMap<>(Map.of("SKU-1", 10, "SKU-2", 10));
        final List<String> reserved = new ArrayList<>();
        final List<String> released = new ArrayList<>();

        @Override
        public void reserve(String sku, int quantity) {
            int left = stock.getOrDefault(sku, 0);
            if (left < quantity) {
                throw new BizException("OUT_OF_STOCK", sku);
            }
            stock.put(sku, left - quantity);
            reserved.add(sku + "x" + quantity);
        }

        @Override
        public void release(String sku, int quantity) {
            stock.merge(sku, quantity, Integer::sum);
            released.add(sku + "x" + quantity);
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

    private final Map<String, BigDecimal> prices = Map.of("SKU-1", new BigDecimal("299.00"), "SKU-2", new BigDecimal("89.90"));
    private final Map<String, Coupon> coupons = Map.of(
            "SAVE20", new Coupon("SAVE20", Coupon.Type.AMOUNT, new BigDecimal("20"), new BigDecimal("100")));
    private final FakeInventory inventory = new FakeInventory();
    private final MemoryOrders orders = new MemoryOrders();
    private final OrderService service = new OrderService(
            sku -> Optional.ofNullable(prices.get(sku)),
            code -> Optional.ofNullable(coupons.get(code)),
            inventory, orders, new PriceCalculator(),
            Clock.fixed(Instant.parse("2026-01-01T00:00:00Z"), ZoneOffset.UTC), null);

    @Test
    void createsOrderWithCoupon() {
        Order o = service.create(new OrderService.CreateOrder("alice",
                List.of(new OrderService.LineRequest("SKU-1", 1)), "SAVE20"));
        assertThat(o.id()).isEqualTo(1L);
        assertThat(o.price().total()).isEqualByComparingTo("279.00");
        assertThat(inventory.reserved).containsExactly("SKU-1x1");
    }

    @Test
    void createsOrderWithoutCoupon() {
        Order o = service.create(new OrderService.CreateOrder("bob",
                List.of(new OrderService.LineRequest("SKU-2", 2)), null));
        assertThat(o.price().total()).isEqualByComparingTo("179.80");
    }

    @Test
    void unknownCouponIsBizError() {
        assertThatThrownBy(() -> service.create(new OrderService.CreateOrder("bob",
                List.of(new OrderService.LineRequest("SKU-2", 1)), "NOPE")))
                .isInstanceOf(BizException.class);
    }

    @Test
    void outOfStockIsBizError() {
        assertThatThrownBy(() -> service.create(new OrderService.CreateOrder("bob",
                List.of(new OrderService.LineRequest("SKU-1", 99)), null)))
                .isInstanceOf(BizException.class);
    }

    /** 多行订单：后一行缺货时，前面已预占的库存要还回去，不能凭空少掉。 */
    @Test
    void failedOrderReleasesStockReservedForEarlierLines() {
        assertThatThrownBy(() -> service.create(new OrderService.CreateOrder("bob",
                List.of(new OrderService.LineRequest("SKU-1", 2), new OrderService.LineRequest("SKU-2", 99)), "SAVE20")))
                .isInstanceOf(BizException.class);
        assertThat(inventory.stock).containsEntry("SKU-1", 10);
        assertThat(inventory.released).containsExactly("SKU-1x2");
    }
}
