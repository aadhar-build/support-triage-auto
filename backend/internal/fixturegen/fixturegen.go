// Package fixturegen writes a made-up, twcs-shaped CSV and a matching gold-label
// CSV for parity tests. Every handle, customer id and text is invented template
// text; nothing comes from the real dataset. The same (seed, threads) always gives
// byte-identical output.
package fixturegen

import (
	"bytes"
	"encoding/csv"
	"fmt"
	"math/rand/v2"
	"sort"
	"strconv"
	"time"
)

// Made-up brand handles. They are not real company handles.
var brands = []string{"AcmeFixtureHelp", "BoltFixtureCare", "CirrusFixtureDesk", "DeltaFixtureTeam"}

// Intents in the order of triage.tiers.INTENTS.
var intents = []string{"delivery", "billing", "account_access", "outage_technical", "complaint", "praise", "other"}
var actions = []string{"answer", "ask", "route", "escalate"}

// Two made-up templates per intent; %s is the brand handle.
var templates = map[string][2]string{
	"delivery":         {"@%s my package still has not arrived and the tracking page shows nothing", "@%s where is my parcel, the courier never came"},
	"billing":          {"@%s I was charged twice for one order and need a refund", "@%s the invoice shows a payment I never made"},
	"account_access":   {"@%s I cannot log in to my account, the password reset fails", "@%s locked out again and the verification code never comes"},
	"outage_technical": {"@%s the app is down and shows an error on every screen", "@%s is there an outage? the site keeps crashing"},
	"complaint":        {"@%s this is the worst support I have had, truly awful", "@%s very disappointed, the agent was rude"},
	"praise":           {"@%s thanks for sorting that out so fast, love it", "@%s awesome help today, thank you"},
	"other":            {"@%s what time does the store open on holidays", "@%s do you have a gift card option"},
}

const (
	twcsTime  = "Mon Jan 02 15:04:05 -0700 2006"
	firstID   = 1000000
	badIDBase = 9000000
)

var (
	// 2017-10-04 (ISO 2017-W40) to 2017-11-29 (ISO 2017-W48).
	spanStart = time.Date(2017, 10, 4, 0, 0, 0, 0, time.UTC)
	spanDays  = 56
	// One brand surge of outage threads inside 90 minutes, well after the
	// first thread so it is past the incidents warm-up.
	surgeStart = time.Date(2017, 11, 8, 15, 0, 0, 0, time.UTC)
)

type thread struct {
	order     int
	at        time.Time
	customer  string
	brand     string
	intent    string
	text      string
	action    string
	replyMin  int // 0 = no brand reply
	followMin int // 0 = no customer follow-up
}

// Generate returns twcs.csv and gold.csv bytes for `threads` made-up threads.
func Generate(seed int64, threads int) (twcs []byte, gold []byte, err error) {
	if threads < 1 {
		return nil, nil, fmt.Errorf("threads must be positive, got %d", threads)
	}
	rng := rand.New(rand.NewPCG(uint64(seed), 0x7477637366697874)) // "twcsfixt"
	pool := threads * 3 / 4
	if pool < 1 {
		pool = 1
	}
	surge := threads / 40

	ts := make([]thread, 0, threads)
	for i := 0; i < threads; i++ {
		c := rng.IntN(pool)
		th := thread{order: i, customer: fmt.Sprintf("fx%06d", c), brand: brands[c%len(brands)]}
		if i < surge {
			th.brand = brands[0]
			th.intent = "outage_technical"
			th.at = surgeStart.Add(time.Duration(rng.IntN(90*60)) * time.Second)
		} else {
			th.intent = intents[rng.IntN(len(intents))]
			th.at = spanStart.Add(time.Duration(rng.IntN(spanDays*24*3600)) * time.Second)
		}
		th.text = fmt.Sprintf(templates[th.intent][rng.IntN(2)], th.brand)
		escalate := false
		switch r := rng.IntN(100); {
		case r < 4:
			th.text += " or I will call my lawyer"
			escalate = true
		case r < 10:
			th.text += " please help asap"
		}
		th.action = goldAction(th.intent, escalate)
		if rng.IntN(10) == 0 { // label noise so accuracy is not trivially 1
			th.action = actions[rng.IntN(len(actions))]
		}
		if rng.IntN(5) != 0 {
			th.replyMin = 5 + rng.IntN(115)
			if rng.IntN(10) < 3 {
				th.followMin = 2 + rng.IntN(58)
			}
		}
		ts = append(ts, th)
	}
	sort.SliceStable(ts, func(a, b int) bool {
		if !ts[a].at.Equal(ts[b].at) {
			return ts[a].at.Before(ts[b].at)
		}
		return ts[a].order < ts[b].order
	})

	var tb, gb bytes.Buffer
	tw, gw := csv.NewWriter(&tb), csv.NewWriter(&gb)
	write := func(w *csv.Writer, rec ...string) {
		if err == nil {
			err = w.Write(rec)
		}
	}
	write(tw, "tweet_id", "author_id", "inbound", "created_at", "text", "response_tweet_id", "in_response_to_tweet_id")
	write(gw, "thread_id", "intent", "action", "should_escalate")

	bad := badRows()
	next := firstID
	for i, th := range ts {
		if len(bad) > 0 && i == (len(ts)*(5-len(bad)))/5 {
			write(tw, bad[0]...)
			bad = bad[1:]
		}
		root, reply, follow := next, next+1, next+2
		next += 3
		replyCol := ""
		if th.replyMin > 0 {
			replyCol = strconv.Itoa(reply)
		}
		write(tw, strconv.Itoa(root), th.customer, "True", th.at.Format(twcsTime), th.text, replyCol, "")
		if th.replyMin > 0 {
			replyAt := th.at.Add(time.Duration(th.replyMin) * time.Minute)
			followCol := ""
			if th.followMin > 0 {
				followCol = strconv.Itoa(follow)
			}
			write(tw, strconv.Itoa(reply), th.brand, "False", replyAt.Format(twcsTime),
				"@"+th.customer+" sorry to hear that, please send us a DM with the details", followCol, strconv.Itoa(root))
			if th.followMin > 0 {
				write(tw, strconv.Itoa(follow), th.customer, "True",
					replyAt.Add(time.Duration(th.followMin)*time.Minute).Format(twcsTime),
					"@"+th.brand+" ok, sent you a message", "", strconv.Itoa(reply))
			}
		}
		write(gw, strconv.Itoa(root), th.intent, th.action, strconv.FormatBool(th.action == "escalate"))
	}
	for _, r := range bad {
		write(tw, r...)
	}
	tw.Flush()
	gw.Flush()
	if err == nil {
		err = tw.Error()
	}
	if err == nil {
		err = gw.Error()
	}
	if err != nil {
		return nil, nil, err
	}
	return tb.Bytes(), gb.Bytes(), nil
}

// goldAction mirrors the intended routing for a clean label.
func goldAction(intent string, escalate bool) string {
	switch {
	case escalate:
		return "escalate"
	case intent == "account_access" || intent == "outage_technical":
		return "route"
	case intent == "complaint" || intent == "praise":
		return "answer"
	default:
		return "ask"
	}
}

// badRows are rows the loader skips: a bad inbound value or an unparsable date.
func badRows() [][]string {
	ok := "Tue Oct 10 10:00:00 +0000 2017"
	return [][]string{
		{strconv.Itoa(badIDBase + 1), "fx900001", "maybe", ok, "made-up malformed row", "", ""},
		{strconv.Itoa(badIDBase + 2), "fx900002", "True", "2017-10-11 10:00:00", "made-up malformed row", "", ""},
		{strconv.Itoa(badIDBase + 3), "fx900003", "yes", ok, "made-up malformed row", "", ""},
		{strconv.Itoa(badIDBase + 4), "fx900004", "False", "Foo Oct 12 10:00:00 +0000 2017", "made-up malformed row", "", ""},
	}
}
