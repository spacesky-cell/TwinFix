package com.acme.order.web;

import com.acme.order.domain.Order;
import com.acme.order.service.OrderService;
import org.springframework.http.HttpStatus;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.PathVariable;
import org.springframework.web.bind.annotation.PostMapping;
import org.springframework.web.bind.annotation.RequestBody;
import org.springframework.web.bind.annotation.RequestMapping;
import org.springframework.web.bind.annotation.ResponseStatus;
import org.springframework.web.bind.annotation.RestController;

@RestController
@RequestMapping("/api/orders")
public class OrderController {

    private final OrderService service;

    public OrderController(OrderService service) {
        this.service = service;
    }

    @PostMapping
    @ResponseStatus(HttpStatus.CREATED)
    public Order create(@RequestBody OrderService.CreateOrder req) {
        return service.create(req);
    }

    @GetMapping("/{id}")
    public Order get(@PathVariable long id) {
        return service.get(id);
    }

    /** 支付网关回调：标记订单已支付。 */
    @PostMapping("/{id}/pay")
    public Order pay(@PathVariable long id) {
        return service.confirmPaid(id);
    }
}
