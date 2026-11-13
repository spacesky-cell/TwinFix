package orders

import (
	"errors"
	"fmt"
)

type OrderService struct {
	inventory  *Inventory
	calculator PriceCalculator
	orders     map[string]*Order
	seq        int
}

func NewOrderService(inventory *Inventory) *OrderService {
	return &OrderService{inventory: inventory, orders: map[string]*Order{}}
}

func (s *OrderService) Create(customer string, lines []OrderLine, coupon *Coupon) (*Order, error) {
	if len(lines) == 0 {
		return nil, errors.New("order needs at least one line")
	}
	cart := NewCart(customer, nil)
	for _, line := range lines {
		if err := s.inventory.Reserve(line.SKU, line.Quantity); err != nil {
			for _, reserved := range cart.Lines {
				s.inventory.Release(reserved.SKU, reserved.Quantity)
			}
			return nil, err
		}
		cart.Add(line)
	}
	breakdown := s.calculator.Calculate(cart.Lines, coupon)
	s.seq++
	order := &Order{
		ID:       fmt.Sprintf("o-%d", s.seq),
		Customer: customer,
		Lines:    append([]OrderLine(nil), cart.Lines...),
		Total:    breakdown.Total(),
	}
	s.orders[order.ID] = order
	return order, nil
}

func (s *OrderService) Get(orderID string) *Order {
	return s.orders[orderID]
}
