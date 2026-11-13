package com.acme.inventory.domain;

/** Expected business outcome (HTTP 409): not enough stock. */
public class OutOfStockException extends RuntimeException {

    public OutOfStockException(String sku, int requested, int available) {
        super("insufficient stock for " + sku + ": requested " + requested + ", available " + available);
    }
}
