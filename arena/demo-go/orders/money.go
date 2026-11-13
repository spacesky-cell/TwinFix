package orders

import "fmt"

// Money is an integer number of cents. The demo deliberately avoids floating point.
type Money int64

func (m Money) String() string {
	sign := ""
	value := int64(m)
	if value < 0 {
		sign = "-"
		value = -value
	}
	return fmt.Sprintf("%s%d.%02d", sign, value/100, value%100)
}
