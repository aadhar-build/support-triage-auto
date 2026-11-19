package policy

import (
	"fmt"
	"strings"
	"testing"

	"github.com/aadhar-build/support-triage-auto/backend/internal/parity"
)

// lowerCases are the made-up contextual strings of
// scripts/golden_context_signals.py LOWER_CASES, in the same order.
var lowerCases = []string{"İBM", "ΑΣ", "Σ", "ΑΣΑ", "ΑΣ.", "Α.Σ", "ΑΣ́", "ΆΣ", "ΑΣ Β",
	"ΣΑ", "Α'Σ", "ΑΣ'Α", "ShopCo", "SHOPCO", "ǅ", "ẞ", "K"}

func TestPyLowerGolden(t *testing.T) {
	var b strings.Builder
	for r := rune(0); r <= 0x10FFFF; r++ {
		if r >= 0xD800 && r <= 0xDFFF {
			continue
		}
		low := pyLower(string(r))
		if low == string(r) {
			continue
		}
		fmt.Fprintf(&b, "%04x", r)
		for _, c := range low {
			fmt.Fprintf(&b, " %04x", c)
		}
		b.WriteByte('\n')
	}
	parity.AssertGolden(t, "context_signals/lower.txt", []byte(b.String()))

	var lines []map[string]any
	for _, s := range lowerCases {
		lines = append(lines, map[string]any{"in": s, "lower": pyLower(s)})
	}
	parity.AssertGolden(t, "context_signals/lower_cases.jsonl", jsonLines(lines))
}
