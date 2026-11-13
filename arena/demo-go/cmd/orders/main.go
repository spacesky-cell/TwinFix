package main

import (
	"fmt"
	"net/http"
	"os"
	"time"

	"example.com/demo-go/orders"
)

func main() {
	if len(os.Args) > 1 && os.Args[1] == "-healthcheck" {
		client := http.Client{Timeout: 2 * time.Second}
		response, err := client.Get(fmt.Sprintf("http://127.0.0.1:%d/actuator/health", orders.Port()))
		if err != nil {
			os.Exit(1)
		}
		defer response.Body.Close()
		if response.StatusCode != http.StatusOK {
			os.Exit(1)
		}
		return
	}
	config := orders.Config{Port: orders.Port(), InventoryToken: orders.InventoryToken()}
	if err := orders.Run(config); err != nil {
		fmt.Fprintln(os.Stderr, err)
		os.Exit(1)
	}
}
