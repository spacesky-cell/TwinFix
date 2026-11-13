import type { Coupon, OrderLine, PriceBreakdown } from "./models.ts";

export class PriceCalculator {
  calculate(lines: OrderLine[], coupon: Coupon | null = null): PriceBreakdown {
    const subtotal = lines.reduce((sum, line) => sum + line.unitPrice * line.quantity, 0);
    const discount = this.discountFor(subtotal, coupon);
    return {
      subtotal,
      discount,
      appliedCoupon: discount > 0 && coupon !== null ? coupon.code : null,
      total: subtotal - discount,
    };
  }

  discountFor(subtotal: number, coupon: Coupon | null): number {
    if (coupon === null) {
      return 0;
    }
    const threshold = this.threshold(coupon);
    if (subtotal < threshold) {
      return 0;
    }
    if (coupon.kind === "amount") {
      return Math.min(coupon.value, subtotal);
    }
    return Math.round((subtotal * coupon.value) / 100);
  }

  private threshold(coupon: Coupon): number {
    // 满减券门槛按面值的 2 倍兜底，防止运营把门槛配成 0 导致无门槛立减
    if (coupon.kind === "amount") {
      return Math.max(coupon.threshold ?? 0, coupon.value * 2);
    }
    return coupon.threshold ?? 0;
  }
}
