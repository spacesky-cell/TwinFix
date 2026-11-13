package orders

import "testing"

func TestExplicitLinesAreCopied(t *testing.T) {
	source := []OrderLine{{SKU: "SKU-1", Quantity: 1, UnitPrice: 1000}}
	cart := NewCart("u-1", source)
	cart.Add(OrderLine{SKU: "SKU-2", Quantity: 1, UnitPrice: 1000})
	if len(source) != 1 {
		t.Fatalf("source mutated: %+v", source)
	}
	if len(cart.Lines) != 2 {
		t.Fatalf("cart lines = %d, want 2", len(cart.Lines))
	}
}

func TestAddAppendsToEmptyCart(t *testing.T) {
	cart := NewCart("u-1", nil)
	cart.Add(OrderLine{SKU: "SKU-1", Quantity: 1, UnitPrice: 1000})
	if len(cart.Lines) != 1 || cart.Subtotal() != 1000 {
		t.Fatalf("cart = %+v, want one line worth 10.00", cart)
	}
}

func TestSubtotalCountsQuantities(t *testing.T) {
	cart := NewCart("u-1", []OrderLine{{SKU: "SKU-1", Quantity: 3, UnitPrice: 1000}})
	if cart.Subtotal() != 3000 {
		t.Fatalf("subtotal = %s, want 30.00", cart.Subtotal())
	}
}
