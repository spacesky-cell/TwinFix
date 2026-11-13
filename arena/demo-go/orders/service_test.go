package orders

import "testing"

func TestOutOfStockIsABusinessRejection(t *testing.T) {
	inventory := NewInventoryWith(map[string]int{"SKU-3": 0})
	service := NewOrderService(inventory)
	_, err := service.Create("u-1", []OrderLine{{SKU: "SKU-3", Quantity: 1, UnitPrice: 1000}}, nil)
	if _, ok := err.(*OutOfStock); !ok {
		t.Fatalf("err = %v, want *OutOfStock", err)
	}
	if inventory.Available("SKU-3") != 0 {
		t.Fatalf("stock changed after rejection")
	}
}

func TestZeroQuantityIsRejected(t *testing.T) {
	inventory := NewInventoryWith(map[string]int{"SKU-1": 5})
	service := NewOrderService(inventory)
	if _, err := service.Create("u-1", []OrderLine{{SKU: "SKU-1", Quantity: 0, UnitPrice: 1000}}, nil); err == nil {
		t.Fatal("zero quantity was accepted")
	}
	if inventory.Available("SKU-1") != 5 {
		t.Fatalf("stock changed after rejection")
	}
}

func TestFailedMultiLineOrderReleasesReservedLines(t *testing.T) {
	inventory := NewInventoryWith(map[string]int{"SKU-1": 5, "SKU-3": 0})
	service := NewOrderService(inventory)
	lines := []OrderLine{
		{SKU: "SKU-1", Quantity: 2, UnitPrice: 1000},
		{SKU: "SKU-3", Quantity: 1, UnitPrice: 1000},
	}
	if _, err := service.Create("u-1", lines, nil); err == nil {
		t.Fatal("expected out of stock")
	}
	if inventory.Available("SKU-1") != 5 {
		t.Fatalf("SKU-1 available = %d, want 5", inventory.Available("SKU-1"))
	}
}

func TestCouponDoesNotLeakBetweenOrders(t *testing.T) {
	inventory := NewInventoryWith(map[string]int{"SKU-1": 10, "SKU-2": 10})
	service := NewOrderService(inventory)
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
