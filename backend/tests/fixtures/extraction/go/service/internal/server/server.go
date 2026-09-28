package server

import (
	"fmt"
	"net/http"
	"time"

	"example.com/service/api"
	"example.com/service/internal/store"
	"github.com/acme/retry"
)

type Handler interface {
	Handle(w http.ResponseWriter, r *http.Request)
}

type Server struct {
	store   *store.Store
	handler Handler
	timeout time.Duration
}

func New(s *store.Store, seconds int) *Server {
	srv := &Server{store: s, timeout: time.Duration(seconds) * time.Second}
	srv.handler = srv
	return srv
}

func (s *Server) Handle(w http.ResponseWriter, r *http.Request) {
	value, err := s.store.Get(r.URL.Query().Get("key"))
	if err != nil {
		http.Error(w, err.Error(), http.StatusNotFound)
		return
	}
	fmt.Fprintln(w, api.Render(value))
}

func (s *Server) Start(addr string) error {
	return retry.Do(3, func() error {
		return http.ListenAndServe(addr, s.mux())
	})
}

func (s *Server) mux() *http.ServeMux {
	m := http.NewServeMux()
	m.HandleFunc("/", s.handler.Handle)
	s.logStart()
	return m
}
