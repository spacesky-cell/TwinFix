package com.acme.inventory.service;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;

import com.acme.inventory.domain.OutOfStockException;
import com.acme.inventory.infra.StockRepository;
import java.util.Map;
import java.util.OptionalInt;
import java.util.concurrent.ConcurrentHashMap;
import org.junit.jupiter.api.Test;

class StockServiceTest {

    /** In-memory repository with the same atomic semantics as the SQL conditional update. */
    static class MemoryStock implements StockRepository {
        final Map<String, Integer> rows = new ConcurrentHashMap<>(Map.of("SKU-1", 5));

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

    private final MemoryStock repo = new MemoryStock();
    private final StockService service = new StockService(repo);

    @Test
    void reserves() {
        service.reserve("SKU-1", 3);
        assertThat(service.available("SKU-1")).isEqualTo(2);
    }

    @Test
    void rejectsWhenInsufficient() {
        assertThatThrownBy(() -> service.reserve("SKU-1", 6)).isInstanceOf(OutOfStockException.class);
        assertThat(service.available("SKU-1")).isEqualTo(5);
    }

    @Test
    void rejectsNonPositiveQuantity() {
        assertThatThrownBy(() -> service.reserve("SKU-1", 0)).isInstanceOf(IllegalArgumentException.class);
    }
}
