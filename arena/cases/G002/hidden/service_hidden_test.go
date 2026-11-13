package orders

import "testing"

func hiddenService() *OrderService {
	inventory := NewInventoryWith(map[string]int{"SKU-1": 100, "SKU-2": 100})
	return NewOrderService(inventory)
}

func TestHiddenCouponDoesNotLeakToNextOrder(t *testing.T) {
	service := hiddenService()
	coupon := &Coupon{Code: "SAVE20", Kind: "amount", Value: 2000, Threshold: 10000}
	first, err := service.Create("u-1", []OrderLine{{SKU: "SKU-1", Quantity: 1, UnitPrice: 10000}}, coupon)
	if err != nil {
		t.Fatal(err)
	}
	if first.Total != 8000 {
		t.Fatalf("first total = %s, want 80.00", first.Total)
	}
	second, err := service.Create("u-2", []OrderLine{{SKU: "SKU-2", Quantity: 1, UnitPrice: 10000}}, nil)
	if err != nil {
		t.Fatal(err)
	}
	if second.Total != 10000 {
		t.Fatalf("second total = %s, want 100.00 (coupon leaked from previous order)", second.Total)
	}
}

func TestHiddenNoCouponOrderBeforeCouponStillPaysFullPrice(t *testing.T) {
	service := hiddenService()
	first, err := service.Create("u-1", []OrderLine{{SKU: "SKU-1", Quantity: 1, UnitPrice: 10000}}, nil)
	if err != nil {
		t.Fatal(err)
	}
	if first.Total != 10000 {
		t.Fatalf("first total = %s, want 100.00", first.Total)
	}
	coupon := &Coupon{Code: "SAVE20", Kind: "amount", Value: 2000, Threshold: 10000}
	second, err := service.Create("u-2", []OrderLine{{SKU: "SKU-2", Quantity: 1, UnitPrice: 10000}}, coupon)
	if err != nil {
		t.Fatal(err)
	}
	if second.Total != 8000 {
		t.Fatalf("second total = %s, want 80.00", second.Total)
	}
}

func TestHiddenCouponAppliesToItsOwnOrderOnly(t *testing.T) {
	service := hiddenService()
	firstCoupon := &Coupon{Code: "SAVE20", Kind: "amount", Value: 2000, Threshold: 10000}
	secondCoupon := &Coupon{Code: "OFF10", Kind: "percent", Value: 10}
	first, err := service.Create("u-1", []OrderLine{{SKU: "SKU-1", Quantity: 1, UnitPrice: 10000}}, firstCoupon)
	if err != nil {
		t.Fatal(err)
	}
	second, err := service.Create("u-2", []OrderLine{{SKU: "SKU-2", Quantity: 1, UnitPrice: 10000}}, secondCoupon)
	if err != nil {
		t.Fatal(err)
	}
	if first.Total != 8000 {
		t.Fatalf("first total = %s, want 80.00", first.Total)
	}
	if second.Total != 9000 {
		t.Fatalf("second total = %s, want 90.00", second.Total)
	}
}
