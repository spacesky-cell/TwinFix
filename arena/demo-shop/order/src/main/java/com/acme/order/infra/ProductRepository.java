package com.acme.order.infra;

import java.math.BigDecimal;
import java.util.Optional;

public interface ProductRepository {

    Optional<BigDecimal> findPrice(String sku);
}
