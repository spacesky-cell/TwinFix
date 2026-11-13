package orders

type Cart struct {
	Customer string
	Lines    []OrderLine
}

func NewCart(customer string, lines []OrderLine) *Cart {
	copied := make([]OrderLine, len(lines))
	copy(copied, lines)
	return &Cart{Customer: customer, Lines: copied}
}

func (c *Cart) Add(line OrderLine) {
	c.Lines = append(c.Lines, line)
}

func (c *Cart) Subtotal() Money {
	var total Money
	for _, line := range c.Lines {
		total += line.UnitPrice * Money(line.Quantity)
	}
	return total
}
