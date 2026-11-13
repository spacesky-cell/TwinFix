package com.acme.order.infra;

import com.acme.order.domain.Order;
import com.acme.order.domain.OrderLine;
import com.acme.order.domain.PriceBreakdown;
import java.sql.Timestamp;
import java.util.List;
import java.util.Optional;
import org.springframework.jdbc.core.simple.JdbcClient;
import org.springframework.jdbc.support.GeneratedKeyHolder;
import org.springframework.transaction.support.TransactionTemplate;

public class JdbcOrderRepository implements OrderRepository {

    private final JdbcClient jdbc;
    private final TransactionTemplate tx;

    public JdbcOrderRepository(JdbcClient jdbc, TransactionTemplate tx) {
        this.jdbc = jdbc;
        this.tx = tx;
    }

    @Override
    public Order save(Order order) {
        return tx.execute(status -> {
            var keys = new GeneratedKeyHolder();
            jdbc.sql("""
                    INSERT INTO orders (customer, subtotal, discount, total, coupon_code, status, created_at)
                    VALUES (:customer, :subtotal, :discount, :total, :coupon, :status, :createdAt)
                    """)
                    .param("customer", order.customer())
                    .param("subtotal", order.price().subtotal())
                    .param("discount", order.price().discount())
                    .param("total", order.price().total())
                    .param("coupon", order.price().appliedCoupon())
                    .param("status", order.status())
                    .param("createdAt", Timestamp.from(order.createdAt()))
                    .update(keys, "id");
            long id = keys.getKey().longValue();
            for (OrderLine line : order.lines()) {
                jdbc.sql("INSERT INTO order_line (order_id, sku, quantity, unit_price) VALUES (:id, :sku, :qty, :price)")
                        .param("id", id)
                        .param("sku", line.sku())
                        .param("qty", line.quantity())
                        .param("price", line.unitPrice())
                        .update();
            }
            return order.withId(id);
        });
    }

    @Override
    public void saveStatus(long id, String status) {
        jdbc.sql("UPDATE orders SET status = :status WHERE id = :id")
                .param("status", status)
                .param("id", id)
                .update();
    }

    @Override
    public Optional<Order> findById(long id) {
        List<OrderLine> lines = jdbc.sql("SELECT sku, quantity, unit_price FROM order_line WHERE order_id = :id ORDER BY id")
                .param("id", id)
                .query((rs, n) -> new OrderLine(rs.getString("sku"), rs.getInt("quantity"), rs.getBigDecimal("unit_price")))
                .list();
        return jdbc.sql("SELECT * FROM orders WHERE id = :id")
                .param("id", id)
                .query((rs, n) -> new Order(
                        rs.getLong("id"),
                        rs.getString("customer"),
                        lines,
                        new PriceBreakdown(rs.getBigDecimal("subtotal"), rs.getBigDecimal("discount"),
                                rs.getBigDecimal("total"), rs.getString("coupon_code")),
                        rs.getString("status"),
                        rs.getTimestamp("created_at").toInstant()))
                .optional();
    }
}
