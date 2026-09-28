package store

import (
	"errors"
	"sync"
)

var ErrNotFound = errors.New("not found")

type Store struct {
	mu   sync.RWMutex
	data map[string]string
}

func New(name string) *Store {
	return &Store{data: make(map[string]string)}
}

func (s *Store) Get(key string) (string, error) {
	s.mu.RLock()
	defer s.mu.RUnlock()
	v, ok := s.data[key]
	if !ok {
		return "", ErrNotFound
	}
	return v, nil
}

func (s *Store) Put(key, value string) {
	s.mu.Lock()
	defer s.mu.Unlock()
	s.data[key] = value
}
