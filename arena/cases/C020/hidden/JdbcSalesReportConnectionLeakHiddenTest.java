package com.acme.order.report;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;

import java.lang.reflect.InvocationHandler;
import java.lang.reflect.Proxy;
import java.math.BigDecimal;
import java.sql.Connection;
import java.sql.PreparedStatement;
import java.sql.ResultSet;
import java.sql.SQLException;
import java.time.Instant;
import java.util.Iterator;
import java.util.List;
import java.util.Map;
import java.util.concurrent.atomic.AtomicInteger;
import javax.sql.DataSource;
import org.junit.jupiter.api.Test;

/**
 * Hidden grading test for C020. JdbcSalesReport borrowed a pooled connection with
 * DataSource.getConnection() and never returned it (always on the error path, and also on the
 * happy path since conn.close() was missing), so the Hikari pool ran dry. Every borrowed
 * connection must be closed on both the success and the SQLException path, and the report must
 * still return correct totals (guards against "fixes" that drop the query).
 */
class JdbcSalesReportConnectionLeakHiddenTest {

    private final AtomicInteger open = new AtomicInteger();
    private final AtomicInteger borrowed = new AtomicInteger();

    @SuppressWarnings("unchecked")
    private static <T> T proxy(Class<T> type, InvocationHandler h) {
        return (T) Proxy.newProxyInstance(type.getClassLoader(), new Class<?>[] {type}, h);
    }

    /** Fake pool: counts connections that were handed out but not closed. */
    private DataSource pool(List<Map.Entry<String, BigDecimal>> data, boolean queryFails) {
        return proxy(DataSource.class, (p, m, a) -> {
            if (!m.getName().equals("getConnection")) {
                return null;
            }
            borrowed.incrementAndGet();
            open.incrementAndGet();
            Iterator<Map.Entry<String, BigDecimal>> it = data.iterator();
            Object[] cur = new Object[1];
            ResultSet rs = proxy(ResultSet.class, (p3, m3, a3) -> switch (m3.getName()) {
                case "next" -> {
                    boolean has = it.hasNext();
                    cur[0] = has ? it.next() : null;
                    yield has;
                }
                case "getString" -> ((Map.Entry<?, ?>) cur[0]).getKey();
                case "getBigDecimal" -> ((Map.Entry<?, ?>) cur[0]).getValue();
                case "getObject" -> a3[0] instanceof Integer i && i == 1
                        ? ((Map.Entry<?, ?>) cur[0]).getKey() : ((Map.Entry<?, ?>) cur[0]).getValue();
                case "isClosed", "wasNull" -> false;
                default -> null;
            });
            PreparedStatement ps = proxy(PreparedStatement.class, (p2, m2, a2) -> {
                if (m2.getName().equals("executeQuery")) {
                    if (queryFails) {
                        throw new SQLException("canceling statement due to statement timeout", "57014");
                    }
                    return rs;
                }
                return m2.getReturnType() == boolean.class ? false : null;
            });
            boolean[] closed = new boolean[1];
            return proxy(Connection.class, (p1, m1, a1) -> switch (m1.getName()) {
                case "prepareStatement" -> ps;
                case "close" -> {
                    if (!closed[0]) {
                        closed[0] = true;
                        open.decrementAndGet();
                    }
                    yield null;
                }
                case "isClosed" -> closed[0];
                case "getAutoCommit" -> true;
                default -> m1.getReturnType() == boolean.class ? false : null;
            });
        });
    }

    private static final Instant FROM = Instant.parse("2026-10-01T00:00:00Z");
    private static final Instant TO = Instant.parse("2026-11-01T00:00:00Z");

    @Test
    void connectionReturnedAfterSuccessfulQuery() {
        var report = new JdbcSalesReport(pool(List.of(
                Map.entry("SKU-1", new BigDecimal("598.00")),
                Map.entry("SKU-3", new BigDecimal("129.50"))), false));
        for (int i = 0; i < 20; i++) {
            assertThat(report.salesBySku(FROM, TO)).containsExactly(
                    Map.entry("SKU-1", new BigDecimal("598.00")),
                    Map.entry("SKU-3", new BigDecimal("129.50")));
        }
        assertThat(borrowed.get()).isPositive();
        assertThat(open.get()).as("connections still checked out of the pool").isZero();
    }

    @Test
    void connectionReturnedWhenQueryFails() {
        var report = new JdbcSalesReport(pool(List.of(), true));
        for (int i = 0; i < 20; i++) {
            assertThatThrownBy(() -> report.salesBySku(FROM, TO)).isInstanceOf(RuntimeException.class);
        }
        assertThat(open.get()).as("connections still checked out of the pool").isZero();
    }
}
