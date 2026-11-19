package policy

import (
	"bufio"
	"os"
	"strconv"
	"strings"
	"testing"
	"unicode"

	"github.com/aadhar-build/support-triage-auto/backend/internal/parity"
)

const maxRune = 0x10FFFF

// TestUnicodeTablesMatchPython walks every code point and compares Go's word
// class, whitespace class and case-insensitive literal matching with the
// tables Python wrote to policy/unicode.txt.
func TestUnicodeTablesMatchPython(t *testing.T) {
	f, err := os.Open(parity.GoldenPath(t, "policy/unicode.txt"))
	if err != nil {
		t.Fatal(err)
	}
	defer f.Close()
	alnum := make([]bool, maxRune+1)
	space := make([]bool, maxRune+1)
	rows := map[rune][]rune{}
	hex := func(s string) rune {
		n, err := strconv.ParseUint(s, 16, 32)
		if err != nil {
			t.Fatalf("unicode.txt: %v", err)
		}
		return rune(n)
	}
	sc := bufio.NewScanner(f)
	for sc.Scan() {
		fields := strings.Fields(sc.Text())
		switch fields[0] {
		case "isalnum", "isspace":
			set := alnum
			if fields[0] == "isspace" {
				set = space
			}
			for r := hex(fields[1]); r <= hex(fields[2]); r++ {
				set[r] = true
			}
		case "case":
			p := hex(fields[1])
			for _, c := range fields[2:] {
				rows[p] = append(rows[p], hex(c))
			}
		default:
			t.Fatalf("unicode.txt: bad line %q", sc.Text())
		}
	}
	if err := sc.Err(); err != nil {
		t.Fatal(err)
	}
	if len(rows) < 2000 {
		t.Fatalf("unicode.txt has only %d case rows", len(rows))
	}

	// Code points grouped by simple lowercase, so each row is built in O(row).
	byLower := map[rune][]rune{}
	for r := rune(0); r <= maxRune; r++ {
		lo := unicode.ToLower(r)
		byLower[lo] = append(byLower[lo], r)
	}
	bad := 0
	report := func(format string, a ...any) {
		if bad++; bad <= 20 {
			t.Errorf(format, a...)
		}
	}
	for r := rune(0); r <= maxRune; r++ {
		if isPyAlnum(r) != alnum[r] {
			report("U+%04X: Go isalnum %v, Python %v", r, isPyAlnum(r), alnum[r])
		}
		if isPySpace(r) != space[r] {
			report("U+%04X: Go isspace %v, Python %v", r, isPySpace(r), space[r])
		}
		m := newCharMatcher(r)
		row, cased := rows[r]
		if m.cased != cased {
			report("U+%04X: Go cased %v, Python %v", r, m.cased, cased)
			continue
		}
		if !cased {
			continue // an uncased literal matches only itself
		}
		for _, c := range row {
			if !m.match(c) {
				report("U+%04X: Go does not match U+%04X; Python does", r, c)
			}
		}
		n := 0
		for _, lo := range m.lows {
			n += len(byLower[lo])
		}
		if n != len(row) {
			report("U+%04X: Go matches %d code points, Python %d", r, n, len(row))
		}
	}
	if bad > 20 {
		t.Errorf("... %d differences in all", bad)
	}
}
