package shapes

import "sort"

func pick(a, b Shape, better func(x, y Shape) bool) Shape {
	if better(a, b) {
		return a
	}
	return b
}

func Sorted(items []Shape) []Shape {
	sort.Slice(items, func(i, j int) bool { return items[i].Area() < items[j].Area() })
	logSorted(len(items))
	return items
}
