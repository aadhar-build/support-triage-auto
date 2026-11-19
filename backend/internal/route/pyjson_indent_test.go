package route

import (
	"math"
	"testing"
)

// Expected bytes are json.dumps(v, sort_keys=True, indent=2) from Python 3.12 on this box.
func TestPyJSONIndent(t *testing.T) {
	var nilStrings []string
	var nilFloat *float64
	cases := []struct {
		name string
		in   any
		want string
	}{
		{"empty containers", map[string]any{"a": []any{}, "b": map[string]any{}, "c": []any{[]any{}}},
			"{\n  \"a\": [],\n  \"b\": {},\n  \"c\": [\n    []\n  ]\n}"},
		{"nested int lists", [][]int{{1, 2}, {3}}, "[\n  [\n    1,\n    2\n  ],\n  [\n    3\n  ]\n]"},
		{"mixed list", []any{nil, "é", 1.0, 1e16}, "[\n  null,\n  \"\\u00e9\",\n  1.0,\n  1e+16\n]"},
		{"nil []string", nilStrings, "[]"},
		{"max uint64", uint64(18446744073709551615), "18446744073709551615"},
		{"nil *float64", nilFloat, "null"},
		{"non-ASCII key", map[string]any{"é": 1}, "{\n  \"\\u00e9\": 1\n}"},
		{"unsorted keys", map[string]any{"z": 1, "a": 2, "m": map[string]any{"y": true, "b": false}},
			"{\n  \"a\": 2,\n  \"m\": {\n    \"b\": false,\n    \"y\": true\n  },\n  \"z\": 1\n}"},
	}
	for _, c := range cases {
		got, err := PyJSONIndent(c.in)
		if err != nil || string(got) != c.want {
			t.Errorf("%s: got %q, %v; want %q", c.name, got, err, c.want)
		}
	}
	if _, err := PyJSONIndent(math.NaN()); err == nil || err.Error() != "pyjson: cannot encode nan" {
		t.Errorf("NaN: got %v", err)
	}
	if _, err := PyJSONIndent(struct{}{}); err == nil || err.Error() != "pyjson: unsupported type struct {}" {
		t.Errorf("struct: got %v", err)
	}
}

func TestPyRepr(t *testing.T) {
	for _, c := range []struct {
		in   any
		want string
	}{{"a", "'a'"}, {nil, "None"}, {1.0, "1.0"}, {"it's", `"it's"`}} {
		if got := PyRepr(c.in); got != c.want {
			t.Errorf("PyRepr(%#v) = %s, want %s", c.in, got, c.want)
		}
	}
}
