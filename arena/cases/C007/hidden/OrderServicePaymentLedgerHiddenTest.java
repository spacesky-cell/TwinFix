package com.acme.order.service;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;

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

/**
 * Hidden grading test for C007. markPaid caught and logged the payment-ledger failure, so the
 * surrounding @Transactional committed the PAID status without a ledger row. The failure must
 * propagate (so the transaction rolls back); the happy path must still record exactly one payment.
 */
class OrderServicePaymentLedgerHiddenTest {

    static class MemoryOrders implements OrderRepository {
        final Map<Long, Order> rows = new HashMap<>();
        final Map<Long, BigDecimal> payments = new HashMap<>();
        boolean ledgerDown;

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
            rows.put(id, new Order(o.id(), o.customer(), o.lines(), o.price(), status, o.createdAt()));
        }

        @Override
        public void recordPayment(long orderId, BigDecimal amount) {
            if (ledgerDown) {
                throw new IllegalStateException("duplicate key value violates unique constraint \"payment_order_id_key\"");
            }
            payments.put(orderId, amount);
        }
    }

    private final MemoryOrders orders = new MemoryOrders();
    private final InventoryGateway inventory = new InventoryGateway() {
        @Override
        public void reserve(String sku, int quantity) {
        }

        @Override
        public void release(String sku, int quantity) {
        }
    };
    private final OrderService service = new OrderService(
            sku -> Optional.of(new BigDecimal("10.00")), code -> Optional.empty(), inventory, orders,
            new PriceCalculator(), Clock.fixed(Instant.parse("2026-01-01T00:00:00Z"), ZoneOffset.UTC), null);

    private long newOrder() {
        return service.create(new OrderService.CreateOrder("u", List.of(new OrderService.LineRequest("SKU-1", 2)), null)).id();
    }

    @Test
    void ledgerFailureMustPropagateSoTransactionRollsBack() {
        long id = newOrder();
        orders.ledgerDown = true;
        assertThatThrownBy(() -> service.confirmPaid(id)).isInstanceOf(RuntimeException.class);
        assertThat(orders.payments).doesNotContainKey(id);
    }

    @Test
    void successfulPaymentRecordsLedgerWithOrderTotal() {
        long id = newOrder();
        Order paid = service.confirmPaid(id);
        assertThat(paid.status()).isEqualTo("PAID");
        assertThat(orders.payments).containsEntry(id, new BigDecimal("20.00"));
    }
}
