package policy

import (
	"fmt"
	"io"
	"math"
	"math/big"
	"slices"
	"strconv"
	"strings"
)

// BrandRulesMain is `triage brand-rules --conformance`: it checks a brand
// file's rules block against a JSON rules file and prints only the names of
// the top-level keys that differ. Exit 0 ok, 1 differ, 2 error.
func BrandRulesMain(args []string, stdout, stderr io.Writer) int {
	usageErr := func(msg string) int {
		fmt.Fprintf(stderr, "error: %s\n", msg)
		return 2
	}
	values := map[string]*string{"--brand": nil, "--rules": nil, "--brands-dir": nil}
	conformance := false
	var unknown []string
	for i := 0; i < len(args); i++ {
		a := args[i]
		if a == "-h" || a == "--help" {
			fmt.Fprintln(stdout, "usage: triage brand-rules [-h] --conformance [--brand BRAND] [--rules RULES] [--brands-dir BRANDS_DIR]")
			return 0
		}
		if a == "--conformance" {
			conformance = true
			continue
		}
		if name, val, eq := strings.Cut(a, "="); eq && name == "--conformance" {
			return usageErr(fmt.Sprintf("argument --conformance: ignored explicit argument %s", reprStr(val)))
		} else if _, ok := values[name]; ok {
			if !eq {
				if i+1 >= len(args) || (strings.HasPrefix(args[i+1], "-") && args[i+1] != "-") {
					return usageErr("argument " + name + ": expected one argument")
				}
				i++
				val = args[i]
			}
			v := val
			values[name] = &v
			continue
		}
		unknown = append(unknown, a)
	}
	if !conformance {
		return usageErr("the following arguments are required: --conformance")
	}
	if len(unknown) > 0 {
		return usageErr("unrecognized arguments: " + strings.Join(unknown, " "))
	}
	brand := "amazonhelp"
	if values["--brand"] != nil {
		brand = *values["--brand"]
	}
	code, msg := conformanceCheck(brand, values["--rules"], values["--brands-dir"])
	if code == 2 {
		fmt.Fprintln(stderr, msg)
	} else {
		fmt.Fprintln(stdout, msg)
	}
	return code
}

// conformanceCheck is Python's conformance(): first error wins, in Python's order.
func conformanceCheck(brand string, rulesFlag, brandsDirFlag *string) (int, string) {
	fail := func(err error) (int, string) { return 2, "error: " + err.Error() }
	path, err := brandPathFor(brand, brandsDirFlag)
	if err != nil {
		return fail(err)
	}
	brandData, _, err := brandObject(path)
	if err != nil {
		return fail(err)
	}
	rulesPath, err := defaultRulesPath(rulesFlag, "policies/rules.json")
	if err != nil {
		return fail(err)
	}
	jsonData, jsonSource, err := readRulesObject(rulesPath)
	if err != nil {
		return fail(err)
	}
	brandEff, err := EffectiveFromData(brandData, brand)
	if err != nil {
		return fail(err)
	}
	jsonEff, err := EffectiveFromData(jsonData, jsonSource)
	if err != nil {
		return fail(err)
	}
	var keys []string
	for _, k := range unionKeys(brandData, jsonData) {
		a, aok := brandData.Get(k)
		b, bok := jsonData.Get(k)
		if aok != bok || !pyEqual(a, b) {
			keys = append(keys, k)
		}
	}
	slices.SortFunc(keys, func(a, b string) int { return slices.Compare([]rune(a), []rune(b)) })
	if len(keys) > 0 || !brandEff.Equal(jsonEff) {
		list := strings.Join(keys, ",")
		if list == "" {
			list = "-"
		}
		return 1, fmt.Sprintf("conformance: differ brand=%s keys=%s", brand, list)
	}
	return 0, "conformance: ok brand=" + brand
}

func unionKeys(a, b Value) []string {
	keys := slices.Clone(a.Keys)
	for _, k := range b.Keys {
		if _, ok := a.Vals[k]; !ok {
			keys = append(keys, k)
		}
	}
	return keys
}

// pyEqual is Python == on two values json.loads produced.
func pyEqual(a, b Value) bool {
	if isNum(a) && isNum(b) {
		return numOf(a).equal(numOf(b))
	}
	if a.Kind != b.Kind {
		return false
	}
	switch a.Kind {
	case Null:
		return true
	case String:
		return a.Str == b.Str
	case Array:
		return slices.EqualFunc(a.Items, b.Items, pyEqual)
	case Object:
		if len(a.Vals) != len(b.Vals) {
			return false
		}
		for k, av := range a.Vals {
			bv, ok := b.Vals[k]
			if !ok || !pyEqual(av, bv) {
				return false
			}
		}
		return true
	}
	return false
}

func isNum(v Value) bool { return v.Kind == Number || v.Kind == Bool }

// numOf is a number or bool as Python sees it: bools are the ints 0 and 1.
func numOf(v Value) pyNum {
	if v.Kind == Bool {
		if v.Bool {
			return pyNumOf("1", true)
		}
		return pyNumOf("0", true)
	}
	return pyNumOf(v.Num, v.IsInt())
}

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
