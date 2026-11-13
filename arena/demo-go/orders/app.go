package orders

import (
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"log"
	"net/http"
	"runtime/debug"
	"strings"
)

type Config struct {
	Port           int
	InventoryToken string
}

type Server struct {
	service *OrderService
	token   string
}

func NewServer(inventory *Inventory, token string) *Server {
	return &Server{service: NewOrderService(inventory), token: token}
}

func (s *Server) Handler() http.Handler {
	mux := http.NewServeMux()
	mux.HandleFunc("/health", s.handleHealth)
	mux.HandleFunc("/actuator/health", s.handleHealth)
	mux.HandleFunc("/api/stock/", s.handleStock)
	mux.HandleFunc("/api/orders", s.handleOrders)
	return mux
}

type couponRequest struct {
	Code      string `json:"code"`
	Kind      string `json:"kind"`
	Value     int64  `json:"value"`
	Threshold int64  `json:"threshold"`
}

type lineRequest struct {
	SKU       string `json:"sku"`
	Quantity  int    `json:"quantity"`
	UnitPrice int64  `json:"unit_price"`
}

type orderRequest struct {
	Customer string         `json:"customer"`
	Lines    []lineRequest  `json:"lines"`
	Coupon   *couponRequest `json:"coupon"`
}

type lineResponse struct {
	SKU       string `json:"sku"`
	Quantity  int    `json:"quantity"`
	UnitPrice string `json:"unit_price"`
}

type orderResponse struct {
	ID       string         `json:"id"`
	Customer string         `json:"customer"`
	Total    string         `json:"total"`
	Lines    []lineResponse `json:"lines"`
}

func (s *Server) handleHealth(w http.ResponseWriter, _ *http.Request) {
	writeJSON(w, http.StatusOK, map[string]string{"status": "UP"})
}

func (s *Server) handleStock(w http.ResponseWriter, r *http.Request) {
	sku := strings.TrimPrefix(r.URL.Path, "/api/stock/")
	if sku == "" || sku == r.URL.Path {
		writeJSON(w, http.StatusNotFound, map[string]string{"error": "not found"})
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{"sku": sku, "available": s.service.inventory.Available(sku)})
}

func (s *Server) handleOrders(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodPost {
		writeJSON(w, http.StatusMethodNotAllowed, map[string]string{"error": "method not allowed"})
		return
	}
	defer func() {
		if recovered := recover(); recovered != nil {
			log.Printf("panic: %v\n%s", recovered, debug.Stack())
			writeJSON(w, http.StatusInternalServerError, map[string]string{
				"code":    "INTERNAL",
				"message": fmt.Sprint(recovered),
			})
		}
	}()

	var body orderRequest
	if err := json.NewDecoder(io.LimitReader(r.Body, 1<<20)).Decode(&body); err != nil {
		writeJSON(w, http.StatusBadRequest, map[string]string{"code": "BAD_REQUEST", "message": err.Error()})
		return
	}
	lines := make([]OrderLine, 0, len(body.Lines))
	for _, raw := range body.Lines {
		lines = append(lines, OrderLine{SKU: raw.SKU, Quantity: raw.Quantity, UnitPrice: Money(raw.UnitPrice)})
	}
	order, err := s.service.Create(body.Customer, lines, toCoupon(body.Coupon))
	if err != nil {
		var outOfStock *OutOfStock
		if errors.As(err, &outOfStock) {
			writeJSON(w, http.StatusConflict, map[string]string{"code": "OUT_OF_STOCK", "message": err.Error()})
			return
		}
		writeJSON(w, http.StatusBadRequest, map[string]string{"code": "BAD_REQUEST", "message": err.Error()})
		return
	}
	writeJSON(w, http.StatusCreated, toResponse(order))
}

func toCoupon(raw *couponRequest) *Coupon {
	if raw == nil {
		return nil
	}
	return &Coupon{
		Code:      raw.Code,
		Kind:      raw.Kind,
		Value:     Money(raw.Value),
		Threshold: Money(raw.Threshold),
	}
}

func toResponse(order *Order) orderResponse {
	lines := make([]lineResponse, 0, len(order.Lines))
	for _, line := range order.Lines {
		lines = append(lines, lineResponse{
			SKU:       line.SKU,
			Quantity:  line.Quantity,
			UnitPrice: line.UnitPrice.String(),
		})
	}
	return orderResponse{ID: order.ID, Customer: order.Customer, Total: order.Total.String(), Lines: lines}
}

func writeJSON(w http.ResponseWriter, status int, payload any) {
	data, err := json.Marshal(payload)
	if err != nil {
		http.Error(w, err.Error(), http.StatusInternalServerError)
		return
	}
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(status)
	_, _ = w.Write(data)
}

func Run(config Config) error {
	server := NewServer(NewInventory(), config.InventoryToken)
	address := fmt.Sprintf(":%d", config.Port)
	log.Printf("orders listening on %s", address)
	return http.ListenAndServe(address, server.Handler())
}
