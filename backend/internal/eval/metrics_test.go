package eval

import "testing"

// Expected messages are Python 3.12 triage.metrics.check_run_at outputs, except
// the two non-ASCII-digit cases, which Go refuses by owner decision Q1.
func TestCheckRunAt(t *testing.T) {
	const bad = "run_at is not a valid time: "
	const format = "run_at must be an ISO 8601 UTC time ending in Z, got "
	cases := []struct{ in, want string }{
		{"2000-01-01T00:00:00Z", ""},
		{"0001-01-01T00:00:00Z", ""},
		{"2024-02-29T00:00:00Z", ""},
		{"2000-01-01T00:00:00.123456789Z", ""},
		{"0000-01-01T00:00:00Z", bad + "'0000-01-01T00:00:00Z'"},
		{"2023-02-29T00:00:00Z", bad + "'2023-02-29T00:00:00Z'"},
		{"2000-01-01T00:00:60Z", bad + "'2000-01-01T00:00:60Z'"},
		{"2000-01-01T00:00:61Z", bad + "'2000-01-01T00:00:61Z'"},
		{"2000-01-01T24:00:00Z", bad + "'2000-01-01T24:00:00Z'"},
		{"2000-13-01T00:00:00Z", bad + "'2000-13-01T00:00:00Z'"},
		{"2000-01-01T00:00:00", format + "'2000-01-01T00:00:00'"},
		{"2000-01-01 00:00:00Z", format + "'2000-01-01 00:00:00Z'"},
		{"2000-01-01T00:00:00.Z", format + "'2000-01-01T00:00:00.Z'"},
		{"2000-01-01T00:00:00Z\n", format + `'2000-01-01T00:00:00Z\n'`},
		// Owner decision Q1: non-ASCII digits are refused (Python accepts them).
		{"２０００-01-01T00:00:00Z", format + "'２０００-01-01T00:00:00Z'"},
		{"2000-01-01T00:00:00.５Z", format + "'2000-01-01T00:00:00.５Z'"},
	}
	for _, c := range cases {
		err := CheckRunAt(c.in)
		got := ""
		if err != nil {
			got = err.Error()
		}
		if got != c.want {
			t.Errorf("CheckRunAt(%q) = %q, want %q", c.in, got, c.want)
		}
	}
}
