package orders

import "fmt"

type OutOfStock struct {
	SKU       string
	Requested int
	Available int
}

func (e *OutOfStock) Error() string {
	return fmt.Sprintf("insufficient stock for %s: requested %d, available %d", e.SKU, e.Requested, e.Available)
}

type Inventory struct {
	stock map[string]int
}

func NewInventory() *Inventory {
	return NewInventoryWith(map[string]int{"SKU-1": 500, "SKU-2": 300, "SKU-3": 0})
}

func NewInventoryWith(stock map[string]int) *Inventory {
	copied := make(map[string]int, len(stock))
	for sku, quantity := range stock {
		copied[sku] = quantity
	}
	return &Inventory{stock: copied}
}

func (i *Inventory) Available(sku string) int {
	return i.stock[sku]
}

func (i *Inventory) Reserve(sku string, quantity int) error {
	if quantity <= 0 {
		return fmt.Errorf("quantity must be positive")
	}
	available := i.Available(sku)
	if available < quantity {
		return &OutOfStock{SKU: sku, Requested: quantity, Available: available}
	}
	i.stock[sku] = available - quantity
	return nil
}

func (i *Inventory) Release(sku string, quantity int) {
	i.stock[sku] = i.Available(sku) + quantity
}
