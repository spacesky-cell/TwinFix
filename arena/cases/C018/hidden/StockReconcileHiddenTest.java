package com.acme.inventory.service;

import static org.assertj.core.api.Assertions.assertThat;

import com.acme.inventory.infra.StockRepository;
import java.util.Collection;
import java.util.HashMap;
import java.util.Map;
import java.util.OptionalInt;
import org.junit.jupiter.api.Test;

/**
 * Hidden grading test for C018. reconcile() compared two boxed Integers with {@code !=}, which is
 * reference comparison: it only happens to work inside the Integer cache (-128..127). Real stock
 * counts above 127 were all reported as mismatched. Values must be compared by value, and real
 * mismatches / unknown SKUs must still be reported.
 */
class StockReconcileHiddenTest {

    static class MemoryStock implements StockRepository {
        final Map<String, Integer> rows = new HashMap<>();

        @Override
        public OptionalInt available(String sku) {
            Integer v = rows.get(sku);
            return v == null ? OptionalInt.empty() : OptionalInt.of(v);
        }

        @Override
        public boolean tryReserve(String sku, int quantity) {
            return false;
        }

        @Override
        public void release(String sku, int quantity) {
            rows.merge(sku, quantity, Integer::sum);
        }

        @Override
        public Map<String, Integer> snapshot(Collection<String> skus) {
            // fresh boxes (outside the Integer cache), as a JDBC row mapper produces them
            Map<String, Integer> out = new HashMap<>();
            for (String sku : skus) {
                Integer v = rows.get(sku);
                if (v != null) {
                    out.put(sku, Integer.valueOf(v.intValue()));
                }
            }
            return out;
        }
    }

    private final MemoryStock repo = new MemoryStock();
    private final StockService service = new StockService(repo);

    @Test
    void equalLargeCountsAreNotMismatched() {
        repo.rows.put("SKU-1", 100);
        repo.rows.put("SKU-2", 500);
        repo.rows.put("SKU-3", 128);
        repo.rows.put("SKU-4", 20_000);
        Map<String, Integer> counted = new HashMap<>();
        counted.put("SKU-1", Integer.valueOf(100));
        counted.put("SKU-2", Integer.valueOf(500));
        counted.put("SKU-3", Integer.valueOf(128));
        counted.put("SKU-4", Integer.valueOf(20_000));
        assertThat(service.reconcile(counted)).isEmpty();
    }

    @Test
    void realMismatchesAndUnknownSkusStillReported() {
        repo.rows.put("SKU-2", 500);
        repo.rows.put("SKU-3", 50);
        Map<String, Integer> counted = new HashMap<>();
        counted.put("SKU-2", 499);
        counted.put("SKU-3", 50);
        counted.put("SKU-X", 7);
        Map<String, Integer> mismatched = service.reconcile(counted);
        assertThat(mismatched).containsOnlyKeys("SKU-2", "SKU-X");
        assertThat(mismatched.get("SKU-2")).isEqualTo(500);
        assertThat(mismatched.get("SKU-X")).isNull();
    }
}
