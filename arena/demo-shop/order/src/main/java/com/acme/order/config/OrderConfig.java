package com.acme.order.config;

import com.acme.order.domain.PriceCalculator;
import com.acme.order.infra.CouponRepository;
import com.acme.order.infra.HttpInventoryGateway;
import com.acme.order.infra.InventoryGateway;
import com.acme.order.infra.JdbcCouponRepository;
import com.acme.order.infra.JdbcOrderRepository;
import com.acme.order.infra.JdbcProductRepository;
import com.acme.order.infra.OrderRepository;
import com.acme.order.infra.ProductRepository;
import com.acme.order.service.OrderService;
import java.time.Clock;
import java.time.Duration;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.context.annotation.Bean;
import org.springframework.context.annotation.Lazy;
import org.springframework.context.annotation.Configuration;
import org.springframework.http.client.SimpleClientHttpRequestFactory;
import org.springframework.jdbc.core.simple.JdbcClient;
import org.springframework.transaction.support.TransactionTemplate;
import org.springframework.web.client.RestClient;

@Configuration
public class OrderConfig {

    @Bean
    ProductRepository productRepository(JdbcClient jdbc) {
        return new JdbcProductRepository(jdbc);
    }

    @Bean
    CouponRepository couponRepository(JdbcClient jdbc) {
        return new JdbcCouponRepository(jdbc);
    }

    @Bean
    OrderRepository orderRepository(JdbcClient jdbc, TransactionTemplate tx) {
        return new JdbcOrderRepository(jdbc, tx);
    }

    @Bean
    InventoryGateway inventoryGateway(@Value("${shop.inventory-url}") String url) {
        var factory = new SimpleClientHttpRequestFactory();
        factory.setConnectTimeout(Duration.ofSeconds(2));
        factory.setReadTimeout(Duration.ofSeconds(5));
        var http = RestClient.builder().baseUrl(url).requestFactory(factory).build();
        return new HttpInventoryGateway(http);
    }

    @Bean
    OrderService orderService(ProductRepository products, CouponRepository coupons, InventoryGateway inventory,
                              OrderRepository orders, @Lazy OrderService self) {
        return new OrderService(products, coupons, inventory, orders, new PriceCalculator(), Clock.systemUTC(), self);
    }
}
