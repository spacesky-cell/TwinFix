package com.acme.order.infra;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;

import com.acme.order.domain.BizException;
import com.acme.order.domain.PriceCalculator;
import com.acme.order.infra.InventoryGateway;
import com.acme.order.service.OrderService;
import java.math.BigDecimal;
import java.time.Clock;
import java.time.Instant;
import java.time.ZoneOffset;
import java.util.List;
import java.util.Map;
import java.util.Optional;
import java.util.concurrent.ConcurrentHashMap;
import java.util.concurrent.atomic.AtomicInteger;
import org.junit.jupiter.api.Test;

/**
 * Hidden grading test for C013. The price cache only stored hits, so every lookup of a non-existent
 * sku went to the database (cache penetration). Repeated lookups of the same unknown sku must reach the
 * delegate once; known skus stay correct and an unknown sku still fails the order with UNKNOWN_SKU.
 */
class CachingProductRepositoryPenetrationHiddenTest {

    private final Map<String, BigDecimal> prices = Map.of("SKU-1", new BigDecimal("299.00"));
    private final Map<String, AtomicInteger> reads = new ConcurrentHashMap<>();
    private final CachingProductRepository repo = new CachingProductRepository(sku -> {
        reads.computeIfAbsent(sku, k -> new AtomicInteger()).incrementAndGet();
        return Optional.ofNullable(prices.get(sku));
    });

    private int readsOf(String sku) {
        AtomicInteger n = reads.get(sku);
        return n == null ? 0 : n.get();
    }

    @Test
    void unknownSkuHitsDatabaseOnce() {
        for (int i = 0; i < 50; i++) {
            assertThat(repo.findPrice("SKU-404")).isEmpty();
        }
        assertThat(readsOf("SKU-404")).isEqualTo(1);
    }

    @Test
    void knownSkuStillCachedAndCorrect() {
        for (int i = 0; i < 5; i++) {
            assertThat(repo.findPrice("SKU-1")).contains(new BigDecimal("299.00"));
        }
        assertThat(readsOf("SKU-1")).isEqualTo(1);
    }

    @Test
    void orderWithUnknownSkuIsStillRejected() {
        OrderService service = new OrderService(repo, code -> Optional.empty(), new InventoryGateway() {
            @Override
            public void reserve(String sku, int quantity) {
            }

            @Override
            public void release(String sku, int quantity) {
            }
        },
                new InMemoryOrders(), new PriceCalculator(),
                Clock.fixed(Instant.parse("2026-01-01T00:00:00Z"), ZoneOffset.UTC), null);
        for (int i = 0; i < 3; i++) {
            assertThatThrownBy(() -> service.create(new OrderService.CreateOrder("u",
                    List.of(new OrderService.LineRequest("SKU-404", 1)), null)))
                    .isInstanceOf(BizException.class)
                    .satisfies(e -> assertThat(((BizException) e).code()).isEqualTo("UNKNOWN_SKU"));
        }
        assertThat(readsOf("SKU-404")).isEqualTo(1);
    }

    static class InMemoryOrders implements OrderRepository {
        @Override
        public com.acme.order.domain.Order save(com.acme.order.domain.Order order) {
            return order.withId(1L);
        }

        @Override
        public Optional<com.acme.order.domain.Order> findById(long id) {
            return Optional.empty();
        }

        @Override
        public void saveStatus(long id, String status) {
        }
    }
}
