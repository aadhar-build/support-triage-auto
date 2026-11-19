package ingest

import (
	"bytes"
	"crypto/sha256"
	"encoding/binary"
	"encoding/json"
	"fmt"
	"math"
	"os"
	"regexp"
	"sort"
	"strconv"
)

// SplitNames is the frozen split order (split.SPLIT_NAMES).
var SplitNames = [3]string{"dev", "eval", "full"}

const sumTolerance = 1e-9

// SplitConfigError reports a missing or malformed split config. Its message
// starts with the fixed wording of triage.split.SplitConfigError.
type SplitConfigError struct{ Msg string }

func (e *SplitConfigError) Error() string { return e.Msg }

func cfgErr(format string, a ...any) error {
	return &SplitConfigError{Msg: fmt.Sprintf(format, a...)}
}

// Fraction is one split's target share. Literal is the JSON literal as written
// (so an integer target such as 1 stays an integer in reports).
type Fraction struct {
	Name    string
	Value   float64
	Literal string
	IsInt   bool
}

// SplitConfig is a validated data/split.json.
type SplitConfig struct {
	Seed      int64
	Fractions [3]Fraction
}

// SplitReport matches triage.split.SplitReport.
type SplitReport struct {
	Seed    int64
	Total   int
	Counts  map[string]int
	Shares  map[string]float64
	Targets [3]Fraction
}

var intLiteral = regexp.MustCompile(`^-?(0|[1-9][0-9]*)$`)

// LoadSplitConfig reads and validates a split config file (read-only).
func LoadSplitConfig(path string) (SplitConfig, error) {
	b, err := os.ReadFile(path)
	if err != nil {
		return SplitConfig{}, cfgErr("cannot read split config %s: %v", path, err)
	}
	return ConfigFromJSON(b)
}

// ConfigFromJSON validates a split config as triage.split.config_from_dict does.
func ConfigFromJSON(b []byte) (SplitConfig, error) {
	dec := json.NewDecoder(bytes.NewReader(b))
	dec.UseNumber()
	var data any
	if err := dec.Decode(&data); err != nil {
		return SplitConfig{}, cfgErr("split config is not valid JSON: %v", err)
	}
	if dec.More() {
		return SplitConfig{}, cfgErr("split config is not valid JSON: trailing data")
	}
	obj, ok := data.(map[string]any)
	if !ok {
		return SplitConfig{}, cfgErr("split config must be a JSON object")
	}
	rawSeed, ok := obj["seed"]
	if !ok {
		return SplitConfig{}, cfgErr("seed is missing")
	}
	num, ok := rawSeed.(json.Number)
	if !ok || !intLiteral.MatchString(string(num)) {
		return SplitConfig{}, cfgErr("seed must be an int, got %v", rawSeed)
	}
	seed, err := strconv.ParseInt(string(num), 10, 64)
	if err != nil {
		return SplitConfig{}, cfgErr("seed must be an int, got %v (outside int64)", rawSeed)
	}
	if u, ok := obj["unit"].(string); !ok || u != "thread" {
		return SplitConfig{}, cfgErr("unit must be 'thread', got %v", obj["unit"])
	}
	rawFr, ok := obj["fractions"]
	if !ok {
		return SplitConfig{}, cfgErr("fractions is missing")
	}
	fr, ok := rawFr.(map[string]any)
	if !ok {
		return SplitConfig{}, cfgErr("fractions must be an object")
	}
	var missing, extra []string
	for _, n := range SplitNames {
		if _, ok := fr[n]; !ok {
			missing = append(missing, n)
		}
	}
	for k := range fr {
		if k != "dev" && k != "eval" && k != "full" {
			extra = append(extra, k)
		}
	}
	sort.Strings(missing)
	sort.Strings(extra)
	if len(missing) > 0 {
		return SplitConfig{}, cfgErr("fractions missing split name(s): %v", missing)
	}
	if len(extra) > 0 {
		return SplitConfig{}, cfgErr("fractions has unknown split name(s): %v", extra)
	}
	cfg := SplitConfig{Seed: seed}
	total := 0.0
	for i, n := range SplitNames {
		v, ok := fr[n].(json.Number)
		if !ok {
			return SplitConfig{}, cfgErr("fraction '%s' must be a number, got %v", n, fr[n])
		}
		f, err := strconv.ParseFloat(string(v), 64)
		if err != nil && !math.IsInf(f, 0) {
			return SplitConfig{}, cfgErr("fraction '%s' must be a number, got %v", n, v)
		}
		if math.IsInf(f, 0) || math.IsNaN(f) {
			return SplitConfig{}, cfgErr("fraction '%s' must be finite, got %v", n, v)
		}
		if f < 0 {
			return SplitConfig{}, cfgErr("fraction '%s' must not be negative, got %v", n, v)
		}
		cfg.Fractions[i] = Fraction{Name: n, Value: f, Literal: string(v), IsInt: intLiteral.MatchString(string(v))}
		total += f
	}
	if math.Abs(total-1) > sumTolerance {
		return SplitConfig{}, cfgErr("fractions sum to %v, expected 1", total)
	}
	return cfg, nil
}

func hashPrefix(seed int64, root uint64) uint64 {
	sum := sha256.Sum256([]byte(strconv.FormatInt(seed, 10) + ":" + strconv.FormatUint(root, 10)))
	return binary.BigEndian.Uint64(sum[:8])
}

// splitForU walks the cumulative fractions in order; u >= every sum falls
// through to the last name.
func splitForU(u float64, cfg SplitConfig) string {
	cumulative := 0.0
	for _, f := range cfg.Fractions {
		cumulative += f.Value
		if u < cumulative {
			return f.Name
		}
	}
	return cfg.Fractions[len(cfg.Fractions)-1].Name
}

func uFromN(n uint64) float64 { return float64(n) / (1 << 64) }

// AssignSplit returns the split name for one thread root id (frozen recipe).
func AssignSplit(root uint64, cfg SplitConfig) string {
	return splitForU(uFromN(hashPrefix(cfg.Seed, root)), cfg)
}

// Splitter assigns unique root ids incrementally.
type Splitter struct {
	cfg     SplitConfig
	mapping map[uint64]uint8
	counts  [3]int
}

// NewSplitter returns an empty Splitter for cfg.
func NewSplitter(cfg SplitConfig) *Splitter {
	return &Splitter{cfg: cfg, mapping: map[uint64]uint8{}}
}

// Add assigns root (once) and returns its split name.
func (s *Splitter) Add(root uint64) string {
	if i, ok := s.mapping[root]; ok {
		return SplitNames[i]
	}
	name := AssignSplit(root, s.cfg)
	for i, n := range SplitNames {
		if n == name {
			s.mapping[root] = uint8(i)
			s.counts[i]++
		}
	}
	return name
}

// Report summarises every unique root added so far.
func (s *Splitter) Report() SplitReport {
	total := len(s.mapping)
	r := SplitReport{Seed: s.cfg.Seed, Total: total, Counts: map[string]int{}, Shares: map[string]float64{}, Targets: s.cfg.Fractions}
	for i, n := range SplitNames {
		r.Counts[n] = s.counts[i]
		if total > 0 {
			r.Shares[n] = float64(s.counts[i]) / float64(total)
		} else {
			r.Shares[n] = 0.0
		}
	}
	return r
}

// SplitThreads assigns each unique root id, as triage.split.split_threads.
func SplitThreads(roots []uint64, cfg SplitConfig) (map[uint64]string, SplitReport) {
	s := NewSplitter(cfg)
	m := map[uint64]string{}
	for _, r := range roots {
		m[r] = s.Add(r)
	}
	return m, s.Report()
}
