package orders

import "testing"

func testLines(unitPrice Money, quantity int) []OrderLine {
	return []OrderLine{{SKU: "SKU-X", Quantity: quantity, UnitPrice: unitPrice}}
}

func TestNoCouponUsesSubtotal(t *testing.T) {
	breakdown := PriceCalculator{}.Calculate(testLines(17980, 1), nil)
	if breakdown.Total() != 17980 {
		t.Fatalf("total = %s, want 179.80", breakdown.Total())
	}
	if breakdown.Discount != 0 || breakdown.AppliedCoupon != "" {
		t.Fatalf("unexpected discount: %+v", breakdown)
	}
}

func TestAmountCouponAppliesAtThreshold(t *testing.T) {
	coupon := &Coupon{Code: "SAVE20", Kind: "amount", Value: 2000, Threshold: 10000}
	breakdown := PriceCalculator{}.Calculate(testLines(10000, 1), coupon)
	if breakdown.Total() != 8000 || breakdown.AppliedCoupon != "SAVE20" {
		t.Fatalf("breakdown = %+v, want total 80.00 with SAVE20", breakdown)
	}
}

func TestAmountCouponRejectedBelowThreshold(t *testing.T) {
	coupon := &Coupon{Code: "SAVE20", Kind: "amount", Value: 2000, Threshold: 10000}
	breakdown := PriceCalculator{}.Calculate(testLines(9999, 1), coupon)
	if breakdown.Total() != 9999 || breakdown.AppliedCoupon != "" {
		t.Fatalf("breakdown = %+v, want no discount below threshold", breakdown)
	}
}

func TestPercentCouponRoundsHalfUp(t *testing.T) {
	coupon := &Coupon{Code: "OFF10", Kind: "percent", Value: 10}
	breakdown := PriceCalculator{}.Calculate(testLines(5000, 1), coupon)
	if breakdown.Total() != 4500 || breakdown.AppliedCoupon != "OFF10" {
		t.Fatalf("breakdown = %+v, want total 45.00", breakdown)
	}
}

func TestAmountCouponThresholdScalesWithFaceValue(t *testing.T) {
	coupon := &Coupon{Code: "BIG", Kind: "amount", Value: 5000, Threshold: 0}
	if total := (PriceCalculator{}).Calculate(testLines(3000, 1), coupon).Total(); total != 3000 {
		t.Fatalf("30.00 order total = %s, want 30.00", total)
	}
	if total := (PriceCalculator{}).Calculate(testLines(10000, 1), coupon).Total(); total != 5000 {
		t.Fatalf("100.00 order total = %s, want 50.00", total)
	}
}
