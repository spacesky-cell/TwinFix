package orders

import (
	"os"
	"strconv"
)

func Port() int {
	raw := os.Getenv("PORT")
	if raw == "" {
		return 8080
	}
	port, err := strconv.Atoi(raw)
	if err != nil {
		return 8080
	}
	return port
}

// InventoryToken is the service-to-service token. The development default is
// overridden by deployment configuration in real environments.
func InventoryToken() string {
	token := os.Getenv("ORDERS_INVENTORY_TOKEN")
	if token == "" {
		token = "dev-inventory-token"
	}
	return token
}
