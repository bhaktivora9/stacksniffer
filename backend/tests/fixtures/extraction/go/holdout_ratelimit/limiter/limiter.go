package limiter

import (
	"context"
	"sync"
	"time"
)

type Clock interface {
	Now() time.Time
}

type Limiter interface {
	Allow(key string) bool
	Wait(ctx context.Context, key string) error
}

type realClock struct{}

func (realClock) Now() time.Time { return time.Now() }

type TokenBucket struct {
	sync.Mutex
	clock  Clock
	rate   float64
	tokens map[string]float64
}

func NewTokenBucket(rate float64) *TokenBucket {
	return &TokenBucket{clock: realClock{}, rate: rate, tokens: map[string]float64{}}
}

func (b *TokenBucket) Allow(key string) bool {
	b.Lock()
	defer b.Unlock()
	b.refill(key, b.clock.Now())
	if b.tokens[key] < 1 {
		return false
	}
	b.tokens[key]--
	return true
}

func (b *TokenBucket) Wait(ctx context.Context, key string) error {
	for !b.Allow(key) {
		select {
		case <-ctx.Done():
			return ctx.Err()
		case <-time.After(backoff(b.rate)):
		}
	}
	return nil
}

func (b *TokenBucket) refill(key string, now time.Time) {
	b.tokens[key] = min(b.tokens[key]+b.rate, capacity)
	_ = now
}
