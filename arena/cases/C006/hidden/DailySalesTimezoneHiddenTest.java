package com.acme.order.report;

import static org.assertj.core.api.Assertions.assertThat;

import com.acme.order.domain.Order;
import com.acme.order.domain.PriceBreakdown;
import java.math.BigDecimal;
import java.time.Instant;
import java.time.LocalDate;
import java.time.ZoneId;
import java.util.List;
import org.junit.jupiter.api.Test;

/**
 * Hidden grading test for C006. businessDay used UTC instead of the shop zone, so orders placed
 * between 00:00 and 08:00 Beijing time were booked on the previous day. Also checks the zone is
 * honored generally (not hard-coded to +8) and that daytime orders are unaffected.
 */
class DailySalesTimezoneHiddenTest {

    private static Order order(String createdAt, String total) {
        BigDecimal t = new BigDecimal(total);
        return new Order(1L, "u", List.of(), new PriceBreakdown(t, BigDecimal.ZERO, t, null), "PAID", Instant.parse(createdAt));
    }

    @Test
    void earlyMorningOrderBelongsToLocalDay() {
        var sales = new DailySales(ZoneId.of("Asia/Shanghai"));
        // 2026-03-11 00:30 北京时间
        assertThat(sales.businessDay(order("2026-03-10T16:30:00Z", "1.00"))).isEqualTo(LocalDate.parse("2026-03-11"));
    }

    @Test
    void totalsSplitAtLocalMidnight() {
        var sales = new DailySales(ZoneId.of("Asia/Shanghai"));
        var totals = sales.totalsByDay(List.of(
                order("2026-03-10T15:59:59Z", "10.00"),   // 03-10 23:59:59 +08
                order("2026-03-10T16:00:00Z", "20.00"),   // 03-11 00:00:00 +08
                order("2026-03-11T03:00:00Z", "5.00")));  // 03-11 11:00 +08
        assertThat(totals)
                .containsEntry(LocalDate.parse("2026-03-10"), new BigDecimal("10.00"))
                .containsEntry(LocalDate.parse("2026-03-11"), new BigDecimal("25.00"))
                .hasSize(2);
    }

    @Test
    void zoneIsConfigurableNotHardCoded() {
        var ny = new DailySales(ZoneId.of("America/New_York"));
        // 2026-03-11 02:00Z = 03-10 22:00 纽约（EDT, -4）
        assertThat(ny.businessDay(order("2026-03-11T02:00:00Z", "1.00"))).isEqualTo(LocalDate.parse("2026-03-10"));
    }
}
