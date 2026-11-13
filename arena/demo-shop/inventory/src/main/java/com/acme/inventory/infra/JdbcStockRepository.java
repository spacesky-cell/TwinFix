package com.acme.inventory.infra;

import java.util.OptionalInt;
import org.springframework.jdbc.core.simple.JdbcClient;

public class JdbcStockRepository implements StockRepository {

    private final JdbcClient jdbc;

    public JdbcStockRepository(JdbcClient jdbc) {
        this.jdbc = jdbc;
    }

    @Override
    public OptionalInt available(String sku) {
        return jdbc.sql("SELECT available FROM stock WHERE sku = :sku")
                .param("sku", sku)
                .query(Integer.class)
                .optional()
                .map(OptionalInt::of)
                .orElse(OptionalInt.empty());
    }

    @Override
    public void release(String sku, int quantity) {
        jdbc.sql("UPDATE stock SET available = available + :q WHERE sku = :sku")
                .param("q", quantity)
                .param("sku", sku)
                .update();
    }

    @Override
    public boolean tryReserve(String sku, int quantity) {
        int updated = jdbc.sql("UPDATE stock SET available = available - :q WHERE sku = :sku AND available >= :q")
                .param("q", quantity)
                .param("sku", sku)
                .update();
        return updated == 1;
    }
}
