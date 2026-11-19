package eval

import "testing"

// Literals from Python's evaluate.wilson_interval on this box.
func TestWilsonInterval(t *testing.T) {
	if _, _, ok := WilsonInterval(0, 0); ok {
		t.Fatal("(0,0) should give no interval")
	}
	cases := []struct {
		s, n      int
		low, high float64
	}{
		{0, 1, 0.0, 0.7934567085261071},
		{1, 1, 0.20654329147389294, 1.0},
		{1, 2, 0.09452865480086614, 0.9054713451991339},
		{0, 30, 0.0, 0.113517091390478},
		{30, 30, 0.8864829086095221, 1.0},
		{7, 9, 0.4525833436728054, 0.9367762376877727},
		{191, 200, 0.9167024063390322, 0.9761477282752958},
	}
	for _, c := range cases {
		low, high, ok := WilsonInterval(c.s, c.n)
		if !ok || low != c.low || high != c.high {
			t.Errorf("WilsonInterval(%d,%d) = %v %v %v, want %v %v", c.s, c.n, low, high, ok, c.low, c.high)
		}
	}
}
