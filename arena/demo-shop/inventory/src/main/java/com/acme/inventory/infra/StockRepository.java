package com.acme.inventory.infra;

import java.util.OptionalInt;

public interface StockRepository {

    OptionalInt available(String sku);

    /** Adds back stock that was reserved but never sold (failed order, cancellation). */
    void release(String sku, int quantity);

    /**
     * Atomically subtracts {@code quantity} if at least that much is available.
     *
     * @return true if the stock was reserved, false if there was not enough
     */
    boolean tryReserve(String sku, int quantity);
}
