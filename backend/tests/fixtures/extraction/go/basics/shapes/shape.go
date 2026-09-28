package shapes

import (
	"fmt"
	"math"
	str "strings"
)

// Shape is anything with an area.
type Shape interface {
	Area() float64
	Name() string
}

type Named interface {
	Shape
	fmt.Stringer
}

type base struct {
	label string
}

func (b base) Name() string { return str.ToUpper(b.label) }

type Circle struct {
	base
	R float64
}

func NewCircle(r float64) *Circle {
	c := &Circle{base: base{label: "circle"}, R: r}
	return c
}

func (c *Circle) Area() float64 { return math.Pi * square(c.R) }

func (c *Circle) String() string {
	return fmt.Sprintf("%s(%.1f)", c.Name(), c.Area())
}

type Rect struct {
	W, H float64
}

func (r Rect) Area() float64 { return r.W * r.H }
func (r Rect) Name() string  { return "rect" }

type Meters float64

func square(x float64) float64 { return x * x }

func Total(shapes ...Shape) float64 {
	sum := 0.0
	for _, s := range shapes {
		sum += s.Area()
	}
	return sum
}

func Describe(s Shape) string {
	var n Named
	if named, ok := s.(Named); ok {
		n = named
		return n.String()
	}
	return s.Name() + ": " + fmt.Sprint(Meters(s.Area()))
}

func Largest(a, b Shape) Shape {
	return pick(a, b, func(x, y Shape) bool { return x.Area() > y.Area() })
}
