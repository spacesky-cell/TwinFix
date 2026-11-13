package com.acme.order.infra;

import java.math.BigDecimal;
import java.util.Optional;
import org.springframework.jdbc.core.simple.JdbcClient;

public class JdbcProductRepository implements ProductRepository {

    private final JdbcClient jdbc;

    public JdbcProductRepository(JdbcClient jdbc) {
        this.jdbc = jdbc;
    }

    @Override
    public Optional<BigDecimal> findPrice(String sku) {
        return jdbc.sql("SELECT price FROM product WHERE sku = :sku")
                .param("sku", sku)
                .query(BigDecimal.class)
                .optional();
    }
}
