package com.acme.inventory.service;

import com.acme.inventory.domain.OutOfStockException;
import com.acme.inventory.infra.StockRepository;
import java.util.NoSuchElementException;

public class StockService {

    private final StockRepository repo;

    public StockService(StockRepository repo) {
        this.repo = repo;
    }

    public int available(String sku) {
        return repo.available(sku).orElseThrow(() -> new NoSuchElementException("unknown sku " + sku));
    }

    public void reserve(String sku, int quantity) {
        if (quantity <= 0) {
            throw new IllegalArgumentException("quantity must be positive");
        }
        // 条件更新在数据库内原子完成，避免并发下先查后改导致超卖
        if (!repo.tryReserve(sku, quantity)) {
            throw new OutOfStockException(sku, quantity, available(sku));
        }
    }

    /** 归还已预占但最终没卖出去的库存（下单失败回滚、取消订单）。 */
    public void release(String sku, int quantity) {
        if (quantity <= 0) {
            throw new IllegalArgumentException("quantity must be positive");
        }
        repo.release(sku, quantity);
    }
}
