package policy

import (
	"math"
	"math/big"
	"strconv"
)

// pyNum is a JSON number (or bool) as a Python int or float.
type pyNum struct {
	isInt bool
	i     *big.Int
	f     float64
}

func pyNumOf(lit string, isInt bool) pyNum {
	if isInt {
		i, ok := new(big.Int).SetString(lit, 10)
		if ok {
			return pyNum{isInt: true, i: i}
		}
	}
	// A range overflow is kept as ±Inf, as Python's float() keeps it.
	f, _ := strconv.ParseFloat(lit, 64)
	return pyNum{f: f}
}

// equal is Python's == between two numbers: exact across int and float.
func (a pyNum) equal(b pyNum) bool {
	switch {
	case a.isInt && b.isInt:
		return a.i.Cmp(b.i) == 0
	case !a.isInt && !b.isInt:
		return a.f == b.f
	case a.isInt:
		return intEqualsFloat(a.i, b.f)
	default:
		return intEqualsFloat(b.i, a.f)
	}
}

func intEqualsFloat(i *big.Int, f float64) bool {
	if math.IsInf(f, 0) || math.IsNaN(f) {
		return false
	}
	return new(big.Float).SetInt(i).Cmp(new(big.Float).SetFloat64(f)) == 0
}

func pyNumberEqual(a, b PyNumber) bool {
	return pyNumOf(a.Lit, a.IsInt).equal(pyNumOf(b.Lit, b.IsInt))
}
