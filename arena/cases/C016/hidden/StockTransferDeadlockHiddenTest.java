package com.acme.inventory.service;

import static org.assertj.core.api.Assertions.assertThat;
import static org.junit.jupiter.api.Assertions.assertTimeoutPreemptively;

import com.acme.inventory.infra.StockRepository;
import java.time.Duration;
import java.util.ArrayList;
import java.util.List;
import java.util.Map;
import java.util.OptionalInt;
import java.util.concurrent.ConcurrentHashMap;
import java.util.concurrent.CountDownLatch;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;
import java.util.concurrent.Future;
import org.junit.jupiter.api.Test;

/**
 * Hidden grading test for C016. transfer() took the per-SKU locks in argument order, so concurrent
 * A→B and B→A transfers deadlocked. Opposite-direction transfers must all finish, and the total
 * stock across both SKUs must be conserved (no "fix" that skips the move or drops the locking).
 */
class StockTransferDeadlockHiddenTest {

    static class MemoryStock implements StockRepository {
        final Map<String, Integer> rows = new ConcurrentHashMap<>(Map.of("SKU-A", 1_000_000, "SKU-B", 1_000_000));

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
        public synchronized void restock(String sku, int quantity) {
            rows.merge(sku, quantity, Integer::sum);
        }

        @Override
        public synchronized void release(String sku, int quantity) {
            rows.merge(sku, quantity, Integer::sum);
        }
    }

    @Test
    void oppositeTransfersDoNotDeadlockAndConserveStock() {
        MemoryStock repo = new MemoryStock();
        StockService service = new StockService(repo);
        int threadsPerDirection = 4;
        int rounds = 20_000;
        ExecutorService pool = Executors.newFixedThreadPool(threadsPerDirection * 2, r -> {
            Thread t = new Thread(r);
            t.setDaemon(true); // a deadlocked run must not keep the test JVM alive
            return t;
        });
        try {
            assertTimeoutPreemptively(Duration.ofSeconds(20), () -> {
                CountDownLatch start = new CountDownLatch(1);
                List<Future<?>> futures = new ArrayList<>();
                for (int i = 0; i < threadsPerDirection * 2; i++) {
                    boolean forward = i % 2 == 0;
                    futures.add(pool.submit(() -> {
                        start.await();
                        for (int n = 0; n < rounds; n++) {
                            if (forward) {
                                service.transfer("SKU-A", "SKU-B", 1);
                            } else {
                                service.transfer("SKU-B", "SKU-A", 1);
                            }
                        }
                        return null;
                    }));
                }
                start.countDown();
                for (Future<?> f : futures) {
                    f.get();
                }
            });
        } finally {
            pool.shutdownNow();
        }
        assertThat(service.available("SKU-A") + service.available("SKU-B")).isEqualTo(2_000_000);
        assertThat(service.available("SKU-A")).isEqualTo(1_000_000);
    }
}
