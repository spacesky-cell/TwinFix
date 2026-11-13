package orders

type PriceCalculator struct{}

func (c PriceCalculator) Calculate(lines []OrderLine, coupon *Coupon) PriceBreakdown {
	var subtotal Money
	for _, line := range lines {
		subtotal += line.UnitPrice * Money(line.Quantity)
	}
	discount := c.DiscountFor(subtotal, coupon)
	applied := ""
	if discount > 0 && coupon != nil {
		applied = coupon.Code
	}
	return PriceBreakdown{Subtotal: subtotal, Discount: discount, AppliedCoupon: applied}
}

func (c PriceCalculator) DiscountFor(subtotal Money, coupon *Coupon) Money {
	if coupon == nil {
		return 0
	}
	threshold := c.threshold(coupon)
	if subtotal < threshold {
		return 0
	}
	switch coupon.Kind {
	case "amount":
		if coupon.Value >= subtotal {
			return subtotal
		}
		return coupon.Value
	case "percent":
		// percentage value is expressed in whole percent (10 = 10%); round half up to one cent
		return Money((int64(subtotal)*int64(coupon.Value) + 50) / 100)
	default:
		return 0
	}
}

func (c PriceCalculator) threshold(coupon *Coupon) Money {
	// 满减券门槛按面值的 2 倍兜底，防止运营把门槛配成 0 导致无门槛立减
	if coupon.Kind == "amount" && coupon.Value*2 > coupon.Threshold {
		return coupon.Value * 2
	}
	return coupon.Threshold
}
