package com.acme.inventory.infra;

import static org.assertj.core.api.Assertions.assertThat;

import java.util.List;
import java.util.OptionalInt;
import java.util.concurrent.Callable;
import java.util.concurrent.CountDownLatch;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;
import java.util.concurrent.Future;
import org.junit.jupiter.api.Test;

/**
 * Hidden grading test for C004. The buggy tryReserve did SELECT-then-UPDATE (check-then-act),
 * which oversells under concurrency. Grades on the StockRepository contract — the same interface
 * StockService depends on — plus a source-level guard that the JDBC deduction stays a single
 * conditional UPDATE rather than a read-then-write pair.
 */
class StockRepositoryConcurrencyHiddenTest {

    private static final int INITIAL = 50;
    private static final int EACH = 40;

    /** Drives a StockRepository through the same interleaving the race produced. */
    private static long concurrentReserves(StockRepository repo, int threads) throws Exception {
        ExecutorService pool = Executors.newFixedThreadPool(threads);
        CountDownLatch start = new CountDownLatch(1);
        Callable<Boolean> task = () -> {
            start.await();
            Thread.yield();
            return repo.tryReserve("SKU-3", EACH);
        };
        List<Callable<Boolean>> all = java.util.Collections.nCopies(threads, task);
        var futures = pool.invokeAll(all);
        start.countDown();
        long ok = 0;
        for (var f : futures) {
            if (f.get()) {
                ok++;
            }
        }
        pool.shutdown();
        return ok;
    }

    @Test
    void contractTestAgainstService() {
        // StockService must still reject when stock insufficient and accept when sufficient.
        var service = new com.acme.inventory.service.StockService(new StockRepository() {
            private int stock = INITIAL;
            @Override public OptionalInt available(String sku) { return OptionalInt.of(stock); }
            @Override public synchronized boolean tryReserve(String sku, int q) {
                if (stock < q) return false;
                stock -= q;
                return true;
            }
            @Override public synchronized void release(String sku, int q) { stock += q; }
        });
        service.reserve("SKU-3", 40);
        org.assertj.core.api.Assertions.assertThatThrownBy(() -> service.reserve("SKU-3", 40))
                .isInstanceOf(com.acme.inventory.domain.OutOfStockException.class);
    }

    @Test
    void jdbcDeductionIsSingleConditionalUpdate() throws Exception {
        // The fixed implementation must keep the atomic guard:
        //   UPDATE stock SET available = available - :q WHERE sku = :sku AND available >= :q
        // A separate SELECT inside tryReserve reintroduces the race even if unit tests pass.
        String src = java.nio.file.Files.readString(
                java.nio.file.Path.of("src/main/java/com/acme/inventory/infra/JdbcStockRepository.java"));
        String tryReserveBody = src.substring(src.indexOf("boolean tryReserve"));
        assertThat(tryReserveBody).contains("available >= :q");
        assertThat(tryReserveBody).doesNotContain("SELECT available");
    }
}
