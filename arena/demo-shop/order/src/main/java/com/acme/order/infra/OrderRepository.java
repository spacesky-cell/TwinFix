package com.acme.order.infra;

import com.acme.order.domain.Order;
import java.util.Optional;

public interface OrderRepository {

    Order save(Order order);

    Optional<Order> findById(long id);

    void saveStatus(long id, String status);
}
