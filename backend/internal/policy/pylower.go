package policy

import (
	"strings"
	"unicode"
)

// pyLower is Python 3.12's str.lower(): the simple lowercase mapping plus
// Python's full mappings, which are U+0130 -> "i̇" and the context-dependent
// final sigma.
func pyLower(s string) string {
	rs := []rune(s)
	var b strings.Builder
	for i, r := range rs {
		switch r {
		case 0x130:
			b.WriteString("i̇")
		case 0x3A3:
			if finalSigma(rs, i) {
				b.WriteRune(0x3C2)
			} else {
				b.WriteRune(0x3C3)
			}
		default:
			b.WriteRune(unicode.ToLower(r))
		}
	}
	return b.String()
}

// finalSigma is the Unicode Final_Sigma condition as CPython checks it: a
// cased letter before i and none after it, skipping case-ignorables both ways.
func finalSigma(rs []rune, i int) bool {
	j := i - 1
	for j >= 0 && isCaseIgnorable(rs[j]) {
		j--
	}
	if j < 0 || !isCased(rs[j]) {
		return false
	}
	j = i + 1
	for j < len(rs) && isCaseIgnorable(rs[j]) {
		j++
	}
	return j == len(rs) || !isCased(rs[j])
}

// isCased is the Unicode Cased property.
func isCased(r rune) bool {
	return unicode.In(r, unicode.Lu, unicode.Ll, unicode.Lt, unicode.Other_Lowercase, unicode.Other_Uppercase)
}

// isCaseIgnorable is the Unicode Case_Ignorable property: Mn, Me, Cf, Lm,
// Sk, or Word_Break MidLetter, MidNumLet or Single_Quote.
func isCaseIgnorable(r rune) bool {
	switch r {
	case 0x27, 0x2E, 0x3A, 0xB7, 0x387, 0x55F, 0x5F4, 0x2018, 0x2019, 0x2024, 0x2027,
		0xFE13, 0xFE52, 0xFE55, 0xFF07, 0xFF0E, 0xFF1A:
		return true
	}
	return unicode.In(r, unicode.Mn, unicode.Me, unicode.Cf, unicode.Lm, unicode.Sk)
}
