package com.acme.order.infra;

import com.acme.order.domain.Coupon;
import java.util.Optional;
import org.springframework.jdbc.core.simple.JdbcClient;

public class JdbcCouponRepository implements CouponRepository {

    private final JdbcClient jdbc;

    public JdbcCouponRepository(JdbcClient jdbc) {
        this.jdbc = jdbc;
    }

    @Override
    public Optional<Coupon> findByCode(String code) {
        return jdbc.sql("SELECT code, type, value, threshold FROM coupon WHERE code = :code AND active")
                .param("code", code)
                .query((rs, n) -> new Coupon(
                        rs.getString("code"),
                        Coupon.Type.valueOf(rs.getString("type")),
                        rs.getBigDecimal("value"),
                        rs.getBigDecimal("threshold")))
                .optional();
    }
}
