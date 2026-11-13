package com.acme.order.infra;

/** Reserves stock in the inventory service. Throws BizException(OUT_OF_STOCK) when stock is insufficient. */
public interface InventoryGateway {

    void reserve(String sku, int quantity);

    /** Gives reserved stock back (failed order, cancellation). */
    void release(String sku, int quantity);
}
