package com.acme.order.service;

import com.acme.order.domain.BizException;
import com.acme.order.domain.Coupon;
import com.acme.order.domain.Order;
import com.acme.order.domain.OrderLine;
import com.acme.order.domain.PriceBreakdown;
import com.acme.order.domain.PriceCalculator;
import com.acme.order.infra.CouponRepository;
import com.acme.order.infra.InventoryGateway;
import com.acme.order.infra.OrderRepository;
import com.acme.order.infra.ProductRepository;
import java.math.BigDecimal;
import java.time.Clock;
import java.time.Instant;
import java.util.ArrayList;
import java.util.List;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.context.annotation.Lazy;
import org.springframework.transaction.annotation.Transactional;

public class OrderService {

    private static final Logger log = LoggerFactory.getLogger(OrderService.class);

    public record LineRequest(String sku, int quantity) {
    }

    public record CreateOrder(String customer, List<LineRequest> lines, String couponCode) {
    }

    private final ProductRepository products;
    private final CouponRepository coupons;
    private final InventoryGateway inventory;
    private final OrderRepository orders;
    private final PriceCalculator calculator;
    private final Clock clock;
    // 延迟注入自身代理：create 内部调用 confirmPaid 需经过代理才能让事务注解生效
    private final OrderService self;

    public OrderService(ProductRepository products, CouponRepository coupons, InventoryGateway inventory,
                        OrderRepository orders, PriceCalculator calculator, Clock clock, @Lazy OrderService self) {
        this.products = products;
        this.coupons = coupons;
        this.inventory = inventory;
        this.orders = orders;
        this.calculator = calculator;
        this.clock = clock;
        this.self = self == null ? this : self;
    }

    public Order create(CreateOrder req) {
        if (req.lines() == null || req.lines().isEmpty()) {
            throw new BizException("EMPTY_ORDER", "order has no lines");
        }
        List<OrderLine> lines = req.lines().stream().map(this::priced).toList();
        Coupon coupon = resolveCoupon(req.couponCode());
        PriceBreakdown price = calculator.calculate(lines, coupon);

        List<OrderLine> reserved = new ArrayList<>();
        try {
            for (OrderLine line : lines) {
                inventory.reserve(line.sku(), line.quantity());
                reserved.add(line);
            }
        } catch (RuntimeException e) {
            // 多行订单里后面的行缺货时，前面已占的库存必须还回去，否则库存在"下单失败"里凭空少掉
            compensate(reserved);
            throw e;
        }
        Order saved = orders.save(new Order(null, req.customer(), lines, price, "CREATED", Instant.now(clock)));
        log.info("order {} created for {} total={} coupon={}", saved.id(), req.customer(), price.total(), price.appliedCoupon());
        return saved;
    }

    /** 尽力归还：单行归还失败只记日志，不能让原始异常被覆盖。 */
    private void compensate(List<OrderLine> reserved) {
        for (OrderLine line : reserved) {
            try {
                inventory.release(line.sku(), line.quantity());
            } catch (RuntimeException e) {
                log.warn("stock compensation failed for {} x{}: {}", line.sku(), line.quantity(), e.getMessage());
            }
        }
    }

    public Order get(long id) {
        return orders.findById(id).orElseThrow(() -> new BizException("ORDER_NOT_FOUND", "order " + id + " not found"));
    }

    /** 支付回调：置为 PAID。经 self 调用保证事务注解在代理上生效。 */
    public Order confirmPaid(long id) {
        return self.markPaid(id);
    }

    @Transactional
    public Order markPaid(long id) {
        get(id); // 存在性校验，缺失抛 ORDER_NOT_FOUND 并回滚
        orders.saveStatus(id, "PAID");
        return get(id);
    }

    private OrderLine priced(LineRequest r) {
        BigDecimal price = products.findPrice(r.sku())
                .orElseThrow(() -> new BizException("UNKNOWN_SKU", "unknown sku " + r.sku()));
        return new OrderLine(r.sku(), r.quantity(), price);
    }

    private Coupon resolveCoupon(String code) {
        if (code == null || code.isBlank()) {
            return null;
        }
        return coupons.findByCode(code)
                .orElseThrow(() -> new BizException("INVALID_COUPON", "coupon " + code + " does not exist"));
    }
}
