package policy

import (
	"sort"
	"unicode"
)

// isPyAlnum is Python's str.isalnum() for one code point.
func isPyAlnum(r rune) bool { return unicode.IsLetter(r) || unicode.IsNumber(r) }

// isPyWord is Python re's \w on str patterns: isalnum() or "_".
func isPyWord(r rune) bool { return r == '_' || isPyAlnum(r) }

// isPySpace is Python's str.isspace(), which re's \s uses on str patterns.
// It is neither Go regexp's \s nor unicode.IsSpace.
func isPySpace(r rune) bool {
	switch {
	case r >= '\t' && r <= '\r', r >= 0x1c && r <= 0x20, r == 0x85, r == 0xa0, r == 0x1680,
		r >= 0x2000 && r <= 0x200a, r == 0x2028, r == 0x2029, r == 0x202f, r == 0x205f, r == 0x3000:
		return true
	}
	return false
}

// isPyCased is CPython 3.12's _sre.unicode_iscased.
func isPyCased(r rune) bool {
	i := sort.Search(len(casedRanges), func(i int) bool { return casedRanges[i][1] >= r })
	return i < len(casedRanges) && casedRanges[i][0] <= r
}

// charMatcher matches one text code point against one phrase code point under
// re.IGNORECASE, as CPython 3.12 re/_compiler.py compiles a literal: an uncased
// literal matches only itself; a cased one matches every c whose simple
// lowercase is the literal's lowercase or one of its _EXTRA_CASES.
type charMatcher struct {
	lit   rune
	cased bool
	lows  []rune // lowercase of lit, then its extra cases
}

func newCharMatcher(p rune) charMatcher {
	if !isPyCased(p) {
		return charMatcher{lit: p}
	}
	lo := unicode.ToLower(p)
	return charMatcher{lit: p, cased: true, lows: append([]rune{lo}, extraCases[lo]...)}
}

func (m charMatcher) match(c rune) bool {
	if !m.cased {
		return c == m.lit
	}
	lc := unicode.ToLower(c)
	for _, lo := range m.lows {
		if lc == lo {
			return true
		}
	}
	return false
}
