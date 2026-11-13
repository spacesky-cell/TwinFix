package orders

import "testing"

func hiddenLines(unitPrice Money, quantity int) []OrderLine {
	return []OrderLine{{SKU: "SKU-X", Quantity: quantity, UnitPrice: unitPrice}}
}

func TestHiddenOrderWithoutCouponIsPricedAtSubtotal(t *testing.T) {
	breakdown := PriceCalculator{}.Calculate(hiddenLines(17980, 1), nil)
	if breakdown.Total() != 17980 {
		t.Fatalf("total = %s, want 179.80", breakdown.Total())
	}
	if breakdown.Discount != 0 || breakdown.AppliedCoupon != "" {
		t.Fatalf("unexpected discount: %+v", breakdown)
	}
}

func TestHiddenAmountCouponAtConfiguredThreshold(t *testing.T) {
	coupon := &Coupon{Code: "SAVE20", Kind: "amount", Value: 2000, Threshold: 10000}
	breakdown := PriceCalculator{}.Calculate(hiddenLines(10000, 1), coupon)
	if breakdown.Total() != 8000 || breakdown.AppliedCoupon != "SAVE20" {
		t.Fatalf("breakdown = %+v, want total 80.00 with SAVE20", breakdown)
	}
}

func TestHiddenAmountCouponRejectedBelowThreshold(t *testing.T) {
	coupon := &Coupon{Code: "SAVE20", Kind: "amount", Value: 2000, Threshold: 10000}
	breakdown := PriceCalculator{}.Calculate(hiddenLines(9999, 1), coupon)
	if breakdown.Total() != 9999 || breakdown.AppliedCoupon != "" {
		t.Fatalf("breakdown = %+v, want no discount below threshold", breakdown)
	}
}

func TestHiddenZeroThresholdAmountCouponStillGuarded(t *testing.T) {
	coupon := &Coupon{Code: "ZERO", Kind: "amount", Value: 2000, Threshold: 0}
	if applied := (PriceCalculator{}).Calculate(hiddenLines(3999, 1), coupon).AppliedCoupon; applied != "" {
		t.Fatalf("39.99 order applied %q, want no coupon", applied)
	}
	if total := (PriceCalculator{}).Calculate(hiddenLines(4000, 1), coupon).Total(); total != 2000 {
		t.Fatalf("40.00 order total = %s, want 20.00", total)
	}
}

func TestHiddenPercentCouponUnaffected(t *testing.T) {
	coupon := &Coupon{Code: "OFF10", Kind: "percent", Value: 10}
	breakdown := PriceCalculator{}.Calculate(hiddenLines(5000, 1), coupon)
	if breakdown.Total() != 4500 || breakdown.AppliedCoupon != "OFF10" {
		t.Fatalf("breakdown = %+v, want total 45.00 with OFF10", breakdown)
	}
}
