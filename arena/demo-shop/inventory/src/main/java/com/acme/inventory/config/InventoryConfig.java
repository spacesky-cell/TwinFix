package com.acme.inventory.config;

import com.acme.inventory.infra.JdbcStockRepository;
import com.acme.inventory.infra.StockRepository;
import com.acme.inventory.service.StockService;
import org.springframework.context.annotation.Bean;
import org.springframework.context.annotation.Configuration;
import org.springframework.jdbc.core.simple.JdbcClient;

@Configuration
public class InventoryConfig {

    @Bean
    StockRepository stockRepository(JdbcClient jdbc) {
        return new JdbcStockRepository(jdbc);
    }

    @Bean
    StockService stockService(StockRepository repo) {
        return new StockService(repo);
    }
}
