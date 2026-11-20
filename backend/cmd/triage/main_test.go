package main

import (
	"bytes"
	"testing"
)

func TestDispatch(t *testing.T) {
	for _, c := range []struct {
		args           []string
		stdout, stderr string
		code           int
	}{
		{nil, "", "support-triage-auto backend: no subcommand yet\n", 2},
		{[]string{"bogus"}, "", "unknown subcommand: bogus\n", 2},
		{[]string{"brand-rules"}, "", "error: the following arguments are required: --conformance\n", 2},
	} {
		var out, errb bytes.Buffer
		code := run(c.args, &out, &errb)
		if out.String() != c.stdout || errb.String() != c.stderr || code != c.code {
			t.Errorf("%v: got (%q, %q, %d), want (%q, %q, %d)", c.args, out.String(), errb.String(), code, c.stdout, c.stderr, c.code)
		}
	}
}
