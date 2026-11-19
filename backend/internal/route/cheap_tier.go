package route

import (
	"strings"

	"github.com/aadhar-build/support-triage-auto/backend/internal/ingest"
)

// IntentKeyword is one entry of INTENT_KEYWORDS, kept in Python's dict order.
type IntentKeyword struct {
	Intent   string
	Keywords []string
}

// IntentKeywords is cheap_tier.INTENT_KEYWORDS.
var IntentKeywords = []IntentKeyword{
	{"delivery", []string{"package", "delivery", "shipping", "tracking", "parcel", "delivered", "shipment", "courier"}},
	{"billing", []string{"charged", "refund", "bill", "invoice", "payment", "overcharged", "billing"}},
	{"account_access", []string{"password", "login", "locked out", "log in", "2fa", "sign in", "verification code"}},
	{"outage_technical", []string{"outage", "down", "not working", "error", "crash", "crashing", "bug"}},
	{"complaint", []string{"terrible", "worst", "awful", "disappointed", "rude", "horrible", "unacceptable"}},
	{"praise", []string{"thank you", "thanks", "love", "awesome", "great service", "amazing"}},
}

// IntentPriority breaks ties: earliest wins.
var IntentPriority = []string{"account_access", "outage_technical", "billing", "delivery", "complaint", "praise"}

// URGENT_KEYWORDS and ESCALATE_KEYWORDS.
var (
	UrgentKeywords   = []string{"urgent", "asap", "immediately", "emergency"}
	EscalateKeywords = []string{"lawyer", "legal action", "fraud", "hacked", "stolen"}
)

// Confidences.
const (
	NoMatchConfidence = 0.2
	TieConfidence     = 0.4
)

// ConfidenceByHits maps the winning hit count to a confidence; 3 or more gives 0.9.
var ConfidenceByHits = map[int]float64{1: 0.6, 2: 0.75, 3: 0.9}

// maxN is the longest keyword in words (_MAX_N), computed from the tables.
var maxN = func() int {
	n := 0
	all := append(append([]string{}, UrgentKeywords...), EscalateKeywords...)
	for _, ik := range IntentKeywords {
		all = append(all, ik.Keywords...)
	}
	for _, k := range all {
		n = max(n, len(strings.Fields(k)))
	}
	return n
}()

// tokens is _TOKEN_RE.findall(text.lower()) for [a-z0-9']+, with Python's
// str.lower() projected onto that alphabet: A-Z lowercase, U+212A (Kelvin)
// lowercases to k, U+0130 lowercases to i + U+0307 (the combining dot ends the
// token), and every other non-ASCII code point is a separator.
func tokens(text string) []string {
	var out []string
	var cur strings.Builder
	flush := func() {
		if cur.Len() > 0 {
			out = append(out, cur.String())
			cur.Reset()
		}
	}
	for _, r := range text {
		switch {
		case r >= 'a' && r <= 'z', r >= '0' && r <= '9', r == '\'':
			cur.WriteRune(r)
		case r >= 'A' && r <= 'Z':
			cur.WriteRune(r + ('a' - 'A'))
		case r == 'K':
			cur.WriteByte('k')
		case r == 'İ':
			cur.WriteByte('i')
			flush()
		default:
			flush()
		}
	}
	flush()
	return out
}

func ngrams(toks []string, n int) map[string]bool {
	grams := map[string]bool{}
	for size := 1; size <= n; size++ {
		for i := 0; i+size <= len(toks); i++ {
			grams[strings.Join(toks[i:i+size], " ")] = true
		}
	}
	return grams
}

// hits counts distinct keywords whose word tuple is a gram.
func hits(grams map[string]bool, keywords []string) int {
	seen := map[string]bool{}
	n := 0
	for _, k := range keywords {
		key := strings.Join(strings.Fields(k), " ")
		if seen[key] {
			continue
		}
		seen[key] = true
		if grams[key] {
			n++
		}
	}
	return n
}

func confidence(top int, tied bool) float64 {
	switch {
	case top == 0:
		return NoMatchConfidence
	case tied:
		return TieConfidence
	}
	return ConfidenceByHits[min(top, 3)]
}

func action(intent, urgency string, escalateHit bool) string {
	switch {
	case escalateHit:
		return "escalate"
	case intent == "account_access" || intent == "outage_technical":
		return "route"
	case intent == "complaint" && urgency == "high":
		return "escalate"
	case intent == "delivery" || intent == "billing" || intent == "other":
		return "ask"
	}
	return "answer"
}

func keywordsFor(intent string) []string {
	for _, ik := range IntentKeywords {
		if ik.Intent == intent {
			return ik.Keywords
		}
	}
	return nil
}

// CheapTier is the rule and keyword baseline.
type CheapTier struct{ price float64 }

// NewCheapTier takes an optional price; without one it uses the placeholder.
func NewCheapTier(price ...float64) *CheapTier {
	return &CheapTier{price: priceOr(PlaceholderPrices["cheap"], price)}
}

func (*CheapTier) Name() string     { return "cheap" }
func (*CheapTier) ModelID() string  { return "keywords" }
func (c *CheapTier) Price() float64 { return c.price }

// Label labels a thread from its inbound turns.
func (*CheapTier) Label(th ingest.Thread) (Labels, error) {
	var texts []string
	for _, t := range th.Turns {
		if t.Inbound {
			texts = append(texts, t.Text)
		}
	}
	grams := ngrams(tokens(strings.Join(texts, "\n")), maxN)

	counts := map[string]int{}
	top := 0
	for _, intent := range IntentPriority {
		counts[intent] = hits(grams, keywordsFor(intent))
		top = max(top, counts[intent])
	}
	var leaders []string
	for _, intent := range IntentPriority {
		if counts[intent] == top {
			leaders = append(leaders, intent)
		}
	}
	intent := "other"
	if top > 0 {
		intent = leaders[0]
	}
	conf := confidence(top, len(leaders) > 1)

	escalateHit := hits(grams, EscalateKeywords) > 0
	urgency := "normal"
	if escalateHit || hits(grams, UrgentKeywords) > 0 {
		urgency = "high"
	} else if intent == "praise" {
		urgency = "low"
	}
	return NewLabels(intent, urgency, action(intent, urgency, escalateHit), conf)
}
