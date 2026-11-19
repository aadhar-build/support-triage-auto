package eval

import "math"

// WilsonInterval is Python's evaluate.wilson_interval: the Wilson 95% score
// interval for successes out of total. ok is false when total is 0. Callers
// guarantee 0 <= successes <= total.
func WilsonInterval(successes, total int) (low, high float64, ok bool) {
	if total == 0 {
		return 0, 0, false
	}
	// z is a variable, not a constant: Go folds constant products exactly,
	// while Python rounds 1.96*1.96 to float64.
	z := 1.96
	n := float64(total)
	p := float64(successes) / n
	z2 := float64(z * z)
	denom := 1 + z2/n
	centre := (p + z2/float64(2*total)) / denom
	half := z * math.Sqrt(float64(p*(1-p))/n+z2/float64(4*total*total)) / denom
	low, high = 0.0, 1.0
	if successes != 0 {
		low = math.Max(0.0, centre-half)
	}
	if successes != total {
		high = math.Min(1.0, centre+half)
	}
	return low, high, true
}
