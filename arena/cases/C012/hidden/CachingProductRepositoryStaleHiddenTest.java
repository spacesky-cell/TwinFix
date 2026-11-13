package com.acme.order.infra;

import static org.assertj.core.api.Assertions.assertThat;

import java.math.BigDecimal;
import java.util.HashMap;
import java.util.Map;
import java.util.Optional;
import java.util.concurrent.atomic.AtomicInteger;
import org.junit.jupiter.api.Test;

/**
 * Hidden grading test for C012. The price cache was never invalidated on updatePrice, so orders kept
 * using the old price until restart. After an update the next read must return the new price, and the
 * cache must still be effective afterwards (guards against "fixing" it by removing the cache).
 */
class CachingProductRepositoryStaleHiddenTest {

    static class CountingRepo implements ProductRepository {
        final Map<String, BigDecimal> rows = new HashMap<>(Map.of("SKU-1", new BigDecimal("299.00")));
        final AtomicInteger reads = new AtomicInteger();

        @Override
        public Optional<BigDecimal> findPrice(String sku) {
            reads.incrementAndGet();
            return Optional.ofNullable(rows.get(sku));
        }

        @Override
        public void updatePrice(String sku, BigDecimal price) {
            rows.put(sku, price);
        }
    }

    private final CountingRepo db = new CountingRepo();
    private final CachingProductRepository repo = new CachingProductRepository(db);

    @Test
    void readAfterUpdateSeesNewPrice() {
        assertThat(repo.findPrice("SKU-1")).contains(new BigDecimal("299.00"));
        repo.updatePrice("SKU-1", new BigDecimal("199.00"));
        assertThat(repo.findPrice("SKU-1")).contains(new BigDecimal("199.00"));
        assertThat(db.rows).containsEntry("SKU-1", new BigDecimal("199.00"));
    }

    @Test
    void cacheStillServesRepeatedReads() {
        repo.findPrice("SKU-1");
        repo.updatePrice("SKU-1", new BigDecimal("199.00"));
        repo.findPrice("SKU-1");
        int afterReload = db.reads.get();
        for (int i = 0; i < 5; i++) {
            assertThat(repo.findPrice("SKU-1")).contains(new BigDecimal("199.00"));
        }
        assertThat(db.reads.get()).isEqualTo(afterReload);
    }
}
