package com.acme.inventory.web;

import com.acme.inventory.domain.OutOfStockException;
import com.acme.inventory.service.StockService;
import java.util.Map;
import java.util.NoSuchElementException;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.http.HttpStatus;
import org.springframework.http.ResponseEntity;
import org.springframework.web.bind.annotation.ExceptionHandler;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.PathVariable;
import org.springframework.web.bind.annotation.PostMapping;
import org.springframework.web.bind.annotation.RequestBody;
import org.springframework.web.bind.annotation.RequestMapping;
import org.springframework.web.bind.annotation.RestController;

@RestController
@RequestMapping("/api/stock")
public class StockController {

    private static final Logger log = LoggerFactory.getLogger(StockController.class);

    public record ReserveRequest(int quantity) {
    }

    private final StockService service;

    public StockController(StockService service) {
        this.service = service;
    }

    @GetMapping("/{sku}")
    public Map<String, Object> get(@PathVariable String sku) {
        return Map.of("sku", sku, "available", service.available(sku));
    }

    @PostMapping("/{sku}/reserve")
    public Map<String, Object> reserve(@PathVariable String sku, @RequestBody ReserveRequest req) {
        service.reserve(sku, req.quantity());
        return Map.of("sku", sku, "reserved", req.quantity());
    }

    /** 归还预占库存：下单失败或取消订单时由订单服务调用。 */
    @PostMapping("/{sku}/release")
    public Map<String, Object> release(@PathVariable String sku, @RequestBody ReserveRequest req) {
        service.release(sku, req.quantity());
        return Map.of("sku", sku, "released", req.quantity());
    }

    @ExceptionHandler(OutOfStockException.class)
    ResponseEntity<Map<String, String>> outOfStock(OutOfStockException e) {
        return ResponseEntity.status(HttpStatus.CONFLICT).body(Map.of("code", "OUT_OF_STOCK", "message", e.getMessage()));
    }

    @ExceptionHandler(NoSuchElementException.class)
    ResponseEntity<Map<String, String>> notFound(NoSuchElementException e) {
        return ResponseEntity.status(HttpStatus.NOT_FOUND).body(Map.of("code", "NOT_FOUND", "message", e.getMessage()));
    }

    @ExceptionHandler(Exception.class)
    ResponseEntity<Map<String, String>> unexpected(Exception e) {
        log.error("unhandled error", e);
        return ResponseEntity.status(HttpStatus.INTERNAL_SERVER_ERROR)
                .body(Map.of("code", "INTERNAL_ERROR", "message", String.valueOf(e.getMessage())));
    }
}
