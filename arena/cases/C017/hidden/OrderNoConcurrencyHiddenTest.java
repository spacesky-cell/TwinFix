package com.acme.order.domain;

import static org.assertj.core.api.Assertions.assertThat;

import java.time.Duration;
import java.time.Instant;
import java.util.ArrayList;
import java.util.List;
import java.util.concurrent.ConcurrentLinkedQueue;
import java.util.concurrent.CountDownLatch;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;
import java.util.concurrent.Future;
import org.junit.jupiter.api.Test;

/**
 * Hidden grading test for C017. OrderNo shared one static SimpleDateFormat across request threads;
 * SimpleDateFormat is not thread-safe, so concurrent format/parse produced wrong days or threw
 * NumberFormatException. Every thread must get exactly the single-threaded answer; the strict
 * day validation and the order-no format must be kept.
 */
class OrderNoConcurrencyHiddenTest {

    @Test
    void concurrentFormatAndParseAreConsistent() throws Exception {
        int threads = 8;
        int rounds = 20_000;
        Instant base = Instant.parse("2020-01-01T12:00:00Z");
        ExecutorService pool = Executors.newFixedThreadPool(threads);
        ConcurrentLinkedQueue<String> errors = new ConcurrentLinkedQueue<>();
        try {
            CountDownLatch start = new CountDownLatch(1);
            List<Future<?>> futures = new ArrayList<>();
            for (int t = 0; t < threads; t++) {
                int offset = t * 397;
                futures.add(pool.submit(() -> {
                    start.await();
                    for (int i = 0; i < rounds && errors.size() < 20; i++) {
                        long id = offset + i;
                        Instant created = base.plus(Duration.ofDays((offset + i) % 3000));
                        Instant day = created.minus(Duration.ofHours(12));
                        try {
                            String no = OrderNo.of(id, created);
                            Instant parsed = OrderNo.dayOf(no);
                            if (!parsed.equals(day) || OrderNo.idOf(no) != id) {
                                errors.add(no + " -> " + parsed + " expected " + day);
                            }
                        } catch (RuntimeException e) {
                            errors.add(created + " -> " + e);
                        }
                    }
                    return null;
                }));
            }
            start.countDown();
            for (Future<?> f : futures) {
                f.get();
            }
        } finally {
            pool.shutdownNow();
        }
        assertThat(errors).isEmpty();
    }

    @Test
    void formatAndStrictValidationUnchanged() {
        assertThat(OrderNo.of(42, Instant.parse("2026-01-01T23:59:59Z"))).isEqualTo("SO20260101-000042");
        assertThat(OrderNo.dayOf("SO20240229-000001")).isEqualTo(Instant.parse("2024-02-29T00:00:00Z"));
        org.assertj.core.api.Assertions.assertThatThrownBy(() -> OrderNo.dayOf("SO20230229-000001"))
                .isInstanceOf(BizException.class);
        org.assertj.core.api.Assertions.assertThatThrownBy(() -> OrderNo.dayOf("SO20261345-000001"))
                .isInstanceOf(BizException.class);
    }
}
