// Package ingest ports the Python reference loader, thread builder and splitter
// (src/triage/loader.py, threads.py, split.py) to Go, streaming the CSV once.
package ingest

import (
	"errors"
	"fmt"
	"io"
	"regexp"
	"strconv"
	"strings"
	"time"
	"unicode"
)

// ExpectedColumns is loader.EXPECTED_COLUMNS, in order.
var ExpectedColumns = [7]string{"tweet_id", "author_id", "inbound", "created_at", "text", "response_tweet_id", "in_response_to_tweet_id"}

// HeaderError is triage.loader.HeaderError: the header is empty or lacks a column.
type HeaderError struct{ Msg string }

func (e *HeaderError) Error() string { return e.Msg }

// ErrIDOverflow aborts a read: an id valid under the Python rule is above 2^64-1.
var ErrIDOverflow = errors.New("tweet id above 2^64-1 is not supported")

// Tweet is one parsed row.
type Tweet struct {
	ID           uint64
	AuthorID     string
	Inbound      bool
	CreatedAt    time.Time
	Text         string
	ResponseIDs  []uint64
	InReplyTo    uint64
	HasInReplyTo bool
}

// LoadStats is triage.loader.LoadStats. SkipReasons holds only reasons seen.
type LoadStats struct {
	RowsRead, RowsParsed, RowsSkipped int
	SkipReasons                       map[string]int
}

// header maps each expected column to its (last) index, plus the record width.
type header struct {
	idx   [7]int
	width int
}

func checkHeader(names []string) (header, error) {
	if len(names) == 0 {
		return header{}, &HeaderError{Msg: "empty file or header: expected columns " + strings.Join(ExpectedColumns[:], ", ")}
	}
	pos := map[string]int{}
	for i, n := range names {
		pos[n] = i
	}
	h := header{width: len(names)}
	var missing []string
	for i, c := range ExpectedColumns {
		p, ok := pos[c]
		if !ok {
			missing = append(missing, c)
		}
		h.idx[i] = p
	}
	if len(missing) > 0 {
		return header{}, &HeaderError{Msg: "header is missing columns: " + strings.Join(missing, ", ")}
	}
	return h, nil
}

// isPySpace is Python's str.isspace for one code point.
func isPySpace(r rune) bool { return unicode.IsSpace(r) || (r >= 0x1c && r <= 0x1f) }

func pyStrip(s string) string { return strings.TrimFunc(s, isPySpace) }

// parseID applies loader._parse_id: strip, then ASCII \d+ with optional \.0+.
// ok is false for an invalid id; overflow is true for a valid id above 2^64-1.
func parseID(s string) (id uint64, ok, overflow bool) {
	s = pyStrip(s)
	i := 0
	for i < len(s) && s[i] >= '0' && s[i] <= '9' {
		i++
	}
	if i == 0 {
		return 0, false, false
	}
	digits, rest := s[:i], s[i:]
	if rest != "" {
		if rest[0] != '.' || len(rest) < 2 || strings.Trim(rest[1:], "0") != "" {
			return 0, false, false
		}
	}
	v, err := strconv.ParseUint(digits, 10, 64)
	if err != nil {
		return 0, true, true
	}
	return v, true, false
}

var dateRE = regexp.MustCompile(`^(?:Mon|Tue|Wed|Thu|Fri|Sat|Sun) (Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec) ([0-9]{1,2}) ([0-9]{2}):([0-9]{2}):([0-9]{2}) ([+-])([0-9]{2})([0-9]{2}) ([0-9]{4})$`)

var months = map[string]time.Month{"Jan": 1, "Feb": 2, "Mar": 3, "Apr": 4, "May": 5, "Jun": 6, "Jul": 7, "Aug": 8, "Sep": 9, "Oct": 10, "Nov": 11, "Dec": 12}

func daysIn(m time.Month, y int) int {
	switch m {
	case 2:
		if y%4 == 0 && (y%100 != 0 || y%400 == 0) {
			return 29
		}
		return 28
	case 4, 6, 9, 11:
		return 30
	}
	return 31
}

// parseDate applies loader._parse_date, checking every range before building
// the time so nothing is normalised.
func parseDate(s string) (time.Time, bool) {
	m := dateRE.FindStringSubmatch(pyStrip(s))
	if m == nil {
		return time.Time{}, false
	}
	n := func(x string) int { v, _ := strconv.Atoi(x); return v }
	mon := months[m[1]]
	day, hh, mm, ss, tzh, tzm, year := n(m[2]), n(m[3]), n(m[4]), n(m[5]), n(m[7]), n(m[8]), n(m[9])
	if tzh > 23 || tzm > 59 || year < 1 || year > 9999 || day < 1 || day > daysIn(mon, year) || hh > 23 || mm > 59 || ss > 59 {
		return time.Time{}, false
	}
	off := tzh*3600 + tzm*60
	if m[6] == "-" {
		off = -off
	}
	return time.Date(year, mon, day, hh, mm, ss, 0, time.FixedZone("", off)), true
}

type skip string

// parseRow runs the row checks in loader._parse_row order. A non-empty skip
// means the row is skipped; err is fatal.
func parseRow(f []string, h header) (Tweet, skip, error) {
	if len(f) != h.width {
		return Tweet{}, "field_count", nil
	}
	get := func(i int) string { return f[h.idx[i]] }
	var t Tweet
	id, ok, over := parseID(get(0))
	if !ok {
		return t, "tweet_id", nil
	}
	if over {
		return t, "", ErrIDOverflow
	}
	t.ID = id
	t.AuthorID = get(1)
	if pyStrip(t.AuthorID) == "" {
		return t, "author_id", nil
	}
	switch strings.ToLower(pyStrip(get(2))) {
	case "true":
		t.Inbound = true
	case "false":
	default:
		return t, "inbound", nil
	}
	var dok bool
	if t.CreatedAt, dok = parseDate(get(3)); !dok {
		return t, "created_at", nil
	}
	t.Text = get(4)
	if resp := get(5); pyStrip(resp) != "" {
		overflow := false
		for _, piece := range strings.Split(resp, ",") {
			rid, ok, over := parseID(piece)
			if !ok {
				return t, "response_tweet_id", nil
			}
			overflow = overflow || over
			t.ResponseIDs = append(t.ResponseIDs, rid)
		}
		if overflow {
			return t, "", ErrIDOverflow
		}
	}
	if irt := get(6); pyStrip(irt) != "" {
		p, ok, over := parseID(irt)
		if !ok {
			return t, "in_response_to_tweet_id", nil
		}
		if over {
			return t, "", ErrIDOverflow
		}
		t.InReplyTo, t.HasInReplyTo = p, true
	}
	return t, "", nil
}

// readTweets streams r, calling emit with each parsed tweet and the byte
// offset of its record. It returns the header for later re-reads.
func readTweets(r io.Reader, emit func(Tweet, int64) error) (header, LoadStats, error) {
	stats := LoadStats{SkipReasons: map[string]int{}}
	cr := newCSVReader(r, 64<<10)
	names, _, err := cr.next()
	if err != nil && err != io.EOF {
		return header{}, stats, err
	}
	h, err := checkHeader(names)
	if err != nil {
		return header{}, stats, err
	}
	for {
		rec, off, err := cr.next()
		if err == io.EOF {
			return h, stats, nil
		}
		if err != nil {
			return h, stats, err
		}
		if len(rec) == 0 {
			continue
		}
		stats.RowsRead++
		t, why, err := parseRow(rec, h)
		if err != nil {
			return h, stats, fmt.Errorf("row %d: %w", stats.RowsRead, err)
		}
		if why != "" {
			stats.RowsSkipped++
			stats.SkipReasons[string(why)]++
			continue
		}
		stats.RowsParsed++
		if err := emit(t, off); err != nil {
			return h, stats, err
		}
	}
}

// ReadTweets streams r one record at a time, as loader.iter_tweets. Bad rows
// are skipped and counted; header, CSV, UTF-8 and id-overflow errors abort.
func ReadTweets(r io.Reader, emit func(Tweet) error) (LoadStats, error) {
	_, st, err := readTweets(r, func(t Tweet, _ int64) error { return emit(t) })
	return st, err
}
