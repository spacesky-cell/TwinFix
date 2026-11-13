package com.acme.order.infra;

import com.acme.order.domain.Coupon;
import java.util.Optional;

public interface CouponRepository {

    Optional<Coupon> findByCode(String code);
}
