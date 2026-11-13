package com.acme.inventory.messaging;

import static org.assertj.core.api.Assertions.assertThat;

import com.acme.inventory.infra.StockRepository;
import com.acme.inventory.service.StockService;
import java.util.HashMap;
import java.util.HashSet;
import java.util.Map;
import java.util.OptionalInt;
import java.util.Set;
import org.junit.jupiter.api.Test;

/**
 * Hidden grading test for C014. The order.created consumer reserved stock on every delivery, but the
 * broker delivers at least once, so a redelivered event deducted stock twice. Redelivery of the same
 * eventId must deduct once; distinct events must each still deduct (guards against "skip everything").
 */
class OrderEventConsumerIdempotencyHiddenTest {

    static class MemoryStock implements StockRepository {
        final Map<String, Integer> rows = new HashMap<>(Map.of("SKU-1", 10));

        @Override
        public OptionalInt available(String sku) {
            Integer v = rows.get(sku);
            return v == null ? OptionalInt.empty() : OptionalInt.of(v);
        }

        @Override
        public synchronized boolean tryReserve(String sku, int quantity) {
            int left = rows.getOrDefault(sku, 0);
            if (left < quantity) {
                return false;
            }
            rows.put(sku, left - quantity);
            return true;
        }

        @Override
        public synchronized void release(String sku, int quantity) {
            rows.merge(sku, quantity, Integer::sum);
        }
    }

    /** Same contract as the SQL INSERT ... ON CONFLICT DO NOTHING. */
    static class MemoryProcessed implements ProcessedEvents {
        final Set<String> ids = new HashSet<>();

        @Override
        public synchronized boolean record(String eventId) {
            return ids.add(eventId);
        }
    }

    private final MemoryStock repo = new MemoryStock();
    private final OrderEventConsumer consumer = new OrderEventConsumer(new StockService(repo), new MemoryProcessed());

    @Test
    void redeliveredEventDeductsOnce() {
        var event = new OrderCreatedEvent("evt-42", 42L, "SKU-1", 3);
        consumer.onOrderCreated(event);
        consumer.onOrderCreated(event);
        consumer.onOrderCreated(event);
        assertThat(repo.rows).containsEntry("SKU-1", 7);
    }

    @Test
    void distinctEventsEachDeduct() {
        consumer.onOrderCreated(new OrderCreatedEvent("evt-1", 1L, "SKU-1", 2));
        consumer.onOrderCreated(new OrderCreatedEvent("evt-2", 2L, "SKU-1", 2));
        assertThat(repo.rows).containsEntry("SKU-1", 6);
    }
}
