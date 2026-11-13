package com.acme.order.notify;

import static org.assertj.core.api.Assertions.assertThat;
import static org.junit.jupiter.api.Assertions.assertTimeoutPreemptively;

import com.acme.order.domain.Order;
import com.acme.order.domain.PriceBreakdown;
import java.math.BigDecimal;
import java.time.Duration;
import java.time.Instant;
import java.util.List;
import java.util.concurrent.CountDownLatch;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.atomic.AtomicInteger;
import org.junit.jupiter.api.Test;

/**
 * Hidden grading test for C023. OrderNotifier used Executors.newFixedThreadPool, whose
 * LinkedBlockingQueue is unbounded: while the SMS gateway hung, every order queued another task
 * (holding the Order) until the heap filled up. With the sender blocked, the backlog must stay
 * bounded, and orderCreated must neither throw nor block the request thread (so CallerRuns or
 * sending synchronously are not acceptable). After the sender recovers, notifications flow again.
 */
class OrderNotifierBoundedQueueHiddenTest {

    private static final int MAX_BACKLOG = 1_000;

    private static Order order(long id) {
        BigDecimal t = new BigDecimal("10.00");
        return new Order(id, "u-" + id, List.of(), new PriceBreakdown(t, BigDecimal.ZERO, t, null), "CREATED", Instant.EPOCH);
    }

    @Test
    void backlogIsBoundedWhileSenderHangs() throws Exception {
        CountDownLatch release = new CountDownLatch(1);
        AtomicInteger delivered = new AtomicInteger();
        NotificationSender hanging = (c, m) -> {
            try {
                release.await();
            } catch (InterruptedException e) {
                Thread.currentThread().interrupt();
            }
            delivered.incrementAndGet();
        };
        try (var notifier = new OrderNotifier(hanging, 4)) {
            assertTimeoutPreemptively(Duration.ofSeconds(5), () -> {
                for (long i = 1; i <= 20_000; i++) {
                    notifier.orderCreated(order(i)); // 不能抛、不能阻塞下单线程
                }
            });
            assertThat(notifier.executor().getQueue().size())
                    .as("queued notifications while the gateway hangs").isLessThanOrEqualTo(MAX_BACKLOG);

            release.countDown();
            long deadline = System.nanoTime() + TimeUnit.SECONDS.toNanos(10);
            while (notifier.executor().getQueue().size() + notifier.executor().getActiveCount() > 0 && System.nanoTime() < deadline) {
                Thread.sleep(20);
            }
            assertThat(delivered.get()).isPositive();

            // getActiveCount() 是近似值：等送达计数稳定一小段时间，避免把最后一条旧积压通知
            // 错算进恢复后的 50 条里（偶发 before+51）。
            int stable = -1;
            long stableSince = System.nanoTime();
            deadline = System.nanoTime() + TimeUnit.SECONDS.toNanos(10);
            while (System.nanoTime() < deadline) {
                int now = delivered.get();
                if (now != stable) {
                    stable = now;
                    stableSince = System.nanoTime();
                } else if (System.nanoTime() - stableSince > TimeUnit.MILLISECONDS.toNanos(300)) {
                    break;
                }
                Thread.sleep(20);
            }
            int before = delivered.get();
            for (long i = 1; i <= 50; i++) {
                notifier.orderCreated(order(100_000 + i));
            }
            deadline = System.nanoTime() + TimeUnit.SECONDS.toNanos(10);
            while (delivered.get() < before + 50 && System.nanoTime() < deadline) {
                Thread.sleep(20);
            }
            // 只断言"恢复后又送达了 50 条"；极晚到的旧积压通知不应让判分偶发失败
            assertThat(delivered.get()).as("notifications after recovery").isGreaterThanOrEqualTo(before + 50);
        }
    }
}
