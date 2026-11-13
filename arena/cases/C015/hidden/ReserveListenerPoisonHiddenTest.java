package com.acme.inventory.messaging;

import static org.assertj.core.api.Assertions.assertThat;

import com.acme.inventory.infra.StockRepository;
import com.acme.inventory.service.StockService;
import java.util.HashMap;
import java.util.Map;
import java.util.OptionalInt;
import org.junit.jupiter.api.Test;

/**
 * Hidden grading test for C015. ReserveListener returned REQUEUE for every unexpected failure, so a
 * message that can never succeed (unknown sku) was redelivered forever. Simulating the broker, a
 * permanently failing message must be dead-lettered within 10 deliveries, while a transient failure
 * must still be retried (not dead-lettered on first failure) and eventually succeed.
 */
class ReserveListenerPoisonHiddenTest {

    static class FlakyStock implements StockRepository {
        final Map<String, Integer> rows = new HashMap<>(Map.of("SKU-1", 5));
        int failuresLeft;

        @Override
        public OptionalInt available(String sku) {
            Integer v = rows.get(sku);
            return v == null ? OptionalInt.empty() : OptionalInt.of(v);
        }

        @Override
        public boolean tryReserve(String sku, int quantity) {
            if (failuresLeft > 0) {
                failuresLeft--;
                throw new IllegalStateException("could not obtain connection");
            }
            int left = rows.getOrDefault(sku, 0);
            if (left < quantity) {
                return false;
            }
            rows.put(sku, left - quantity);
            return true;
        }

        @Override
        public void release(String sku, int quantity) {
            rows.merge(sku, quantity, Integer::sum);
        }
    }

    private final FlakyStock repo = new FlakyStock();
    private final ReserveListener listener = new ReserveListener(new StockService(repo));

    /** Redelivers like the broker until the listener stops asking for REQUEUE; returns the final ack and count. */
    private Object[] deliverUntilSettled(String sku, int max) {
        for (int n = 1; n <= max; n++) {
            Ack ack = listener.handle(new ReserveMessage("m-1", 1L, sku, 2, n));
            if (ack != Ack.REQUEUE) {
                return new Object[] {ack, n};
            }
        }
        return new Object[] {Ack.REQUEUE, max};
    }

    @Test
    void poisonMessageIsDeadLetteredEventually() {
        Object[] r = deliverUntilSettled("SKU-GONE", 10);
        assertThat(r[0]).isEqualTo(Ack.DEAD_LETTER);
    }

    @Test
    void transientFailureIsRetriedThenSucceeds() {
        repo.failuresLeft = 2;
        assertThat(listener.handle(new ReserveMessage("m-1", 1L, "SKU-1", 2, 1))).isEqualTo(Ack.REQUEUE);
        Object[] r = deliverUntilSettled("SKU-1", 10);
        assertThat(r[0]).isEqualTo(Ack.ACK);
        assertThat(repo.rows).containsEntry("SKU-1", 3);
    }
}
