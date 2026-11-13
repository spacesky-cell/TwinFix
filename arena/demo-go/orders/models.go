package orders

type Coupon struct {
	Code      string
	Kind      string // amount | percent
	Value     Money
	Threshold Money
}

type OrderLine struct {
	SKU       string
	Quantity  int
	UnitPrice Money
}

type PriceBreakdown struct {
	Subtotal      Money
	Discount      Money
	AppliedCoupon string
}

func (b PriceBreakdown) Total() Money {
	return b.Subtotal - b.Discount
}

type Order struct {
	ID       string
	Customer string
	Lines    []OrderLine
	Total    Money
}
