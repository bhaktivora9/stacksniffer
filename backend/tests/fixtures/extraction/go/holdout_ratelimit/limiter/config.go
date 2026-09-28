package limiter

import "time"

const capacity = 10.0

func backoff(rate float64) time.Duration {
	return time.Duration(float64(time.Second) / rate)
}

func Chain(limiters ...Limiter) Limiter {
	return chain(limiters)
}

type chain []Limiter

func (c chain) Allow(key string) bool {
	for _, l := range c {
		if !l.Allow(key) {
			return false
		}
	}
	return true
}
