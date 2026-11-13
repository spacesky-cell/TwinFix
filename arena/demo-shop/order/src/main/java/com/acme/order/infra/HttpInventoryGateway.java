package com.acme.order.infra;

import com.acme.order.domain.BizException;
import java.util.Map;
import org.springframework.http.HttpStatus;
import org.springframework.web.client.HttpClientErrorException;
import org.springframework.web.client.RestClient;

public class HttpInventoryGateway implements InventoryGateway {

    private final RestClient http;

    public HttpInventoryGateway(RestClient http) {
        this.http = http;
    }

    @Override
    public void reserve(String sku, int quantity) {
        try {
            http.post()
                    .uri("/api/stock/{sku}/reserve", sku)
                    .body(Map.of("quantity", quantity))
                    .retrieve()
                    .toBodilessEntity();
        } catch (HttpClientErrorException e) {
            if (e.getStatusCode() == HttpStatus.CONFLICT) {
                throw new BizException("OUT_OF_STOCK", "insufficient stock for " + sku);
            }
            throw e;
        }
    }

    @Override
    public void release(String sku, int quantity) {
        http.post()
                .uri("/api/stock/{sku}/release", sku)
                .body(Map.of("quantity", quantity))
                .retrieve()
                .toBodilessEntity();
    }
}
