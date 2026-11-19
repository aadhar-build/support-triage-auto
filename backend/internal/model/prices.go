// Package model is the Go port of src/triage/model_prices.py and
// src/triage/spend_log.py: owner-edited model prices and the durable monthly
// spend log that guards the paid tier's budget.
package model

import (
	"errors"
	"fmt"
	"io/fs"
	"math"
	"os"
	"strconv"
	"unicode/utf8"

	"github.com/aadhar-build/support-triage-auto/backend/internal/policy"
	"github.com/aadhar-build/support-triage-auto/backend/internal/route"
)

// TokenUsage is one call's token counts.
type TokenUsage struct {
	InputTokens, OutputTokens, CacheWriteTokens, CacheReadTokens int64
}

// Band is one price band. A nil MaxInputTokens is the open-ended last band; a
// nil cache price means that token kind has no price.
type Band struct {
	MaxInputTokens        *int64
	Input, Output         float64
	CacheWrite, CacheRead *float64
}

// Prices is a validated price file.
type Prices struct {
	Version string
	Models  map[string][]Band
}

// PricesError: the price file is missing, unreadable or malformed.
type PricesError struct{ Msg string }

func (e *PricesError) Error() string { return e.Msg }

// UnknownBillError: a model or token kind has no price.
type UnknownBillError struct{ Msg string }

func (e *UnknownBillError) Error() string { return e.Msg }

func osDetail(err error) string {
	var pe *fs.PathError
	if errors.As(err, &pe) {
		return pe.Err.Error()
	}
	return err.Error()
}

// number reads a JSON number the way Python's json does: an int literal is an
// int (refused when it does not fit in int64, Owner decision Q2), anything
// else a float. ok is false for a non-number, a bool or an int outside int64.
func number(v policy.Value) (f float64, isInt bool, ok bool) {
	if v.Kind != policy.Number {
		return 0, false, false
	}
	if v.IsInt() {
		n, err := strconv.ParseInt(v.Num, 10, 64)
		if err != nil {
			return 0, true, false
		}
		return float64(n), true, true
	}
	f, _ = strconv.ParseFloat(v.Num, 64) // out of range gives ±Inf, refused as non-finite
	return f, false, true
}

func price(v policy.Value) (float64, bool) {
	f, _, ok := number(v)
	return f, ok && !math.IsInf(f, 0) && !math.IsNaN(f) && f > 0
}

var priceKeys = []struct {
	key      string
	required bool
}{
	{"input_usd_per_mtok", true}, {"output_usd_per_mtok", true},
	{"cache_write_usd_per_mtok", false}, {"cache_read_usd_per_mtok", false},
}

// LoadPrices reads and validates a price file, in Python's check order.
func LoadPrices(path string) (*Prices, error) {
	fail := func(format string, a ...any) error {
		return &PricesError{"price file " + path + ": " + fmt.Sprintf(format, a...)}
	}
	raw, err := os.ReadFile(path)
	if errors.Is(err, fs.ErrNotExist) {
		return nil, fail("not found")
	} else if err != nil {
		return nil, fail("cannot read: %s", osDetail(err))
	}
	data, err := policy.ParseJSON(raw)
	if err != nil || !utf8.Valid(raw) { // Python decodes UTF-8 first; ParseJSON does not check it
		return nil, fail("not valid JSON")
	}
	if data.Kind != policy.Object {
		return nil, fail("top level is not an object")
	}
	version, _ := data.Get("version")
	if version.Kind != policy.String || version.Str == "" {
		return nil, fail("version is missing or empty")
	}
	models, _ := data.Get("models")
	if models.Kind != policy.Object || len(models.Keys) == 0 {
		return nil, fail("models is missing or empty")
	}
	out := &Prices{Version: version.Str, Models: map[string][]Band{}}
	for _, model := range models.Keys {
		name := route.PyRepr(model)
		bands, _ := models.Vals[model].Get("bands")
		if bands.Kind != policy.Array || len(bands.Items) == 0 {
			return nil, fail("model %s has no bands", name)
		}
		var parsed []Band
		var prev *int64
		for i, b := range bands.Items {
			if b.Kind != policy.Object {
				return nil, fail("model %s band %d is not an object", name, i)
			}
			var band Band
			limit, has := b.Get("max_input_tokens")
			if has && limit.Kind == policy.Null {
				if i != len(bands.Items)-1 {
					return nil, fail("model %s band %d: null max_input_tokens is only allowed last", name, i)
				}
			} else {
				f, isInt, ok := number(limit)
				if !has || !ok || !isInt || f < 1 {
					return nil, fail("model %s band %d: max_input_tokens must be a positive int or null", name, i)
				}
				n, _ := strconv.ParseInt(limit.Num, 10, 64)
				if prev != nil && n <= *prev {
					return nil, fail("model %s band %d: bands are out of order", name, i)
				}
				band.MaxInputTokens, prev = &n, &n
			}
			for _, pk := range priceKeys {
				v, has := b.Get(pk.key)
				if !has {
					if pk.required {
						return nil, fail("model %s band %d: %s is missing", name, i, pk.key)
					}
					continue
				}
				f, ok := price(v)
				if !ok {
					return nil, fail("model %s band %d: %s must be a finite number > 0", name, i, pk.key)
				}
				switch pk.key {
				case "input_usd_per_mtok":
					band.Input = f
				case "output_usd_per_mtok":
					band.Output = f
				case "cache_write_usd_per_mtok":
					band.CacheWrite = &f
				default:
					band.CacheRead = &f
				}
			}
			parsed = append(parsed, band)
		}
		out.Models[model] = parsed
	}
	return out, nil
}

// Band returns the first band whose limit is null or >= inputTokens.
func (p *Prices) Band(model string, inputTokens int64) (Band, error) {
	bands := p.Models[model]
	if len(bands) == 0 {
		return Band{}, &UnknownBillError{fmt.Sprintf("model %s has no price", route.PyRepr(model))}
	}
	for _, b := range bands {
		if b.MaxInputTokens == nil || *b.MaxInputTokens >= inputTokens {
			return b, nil
		}
	}
	return Band{}, &UnknownBillError{fmt.Sprintf("model %s has no band for %d input tokens", route.PyRepr(model), inputTokens)}
}

// Cost is the USD for one model's usage, with Python's arithmetic order.
func (p *Prices) Cost(model string, u TokenUsage) (float64, error) {
	band, err := p.Band(model, u.InputTokens+u.CacheWriteTokens+u.CacheReadTokens)
	if err != nil {
		return 0, err
	}
	usd := 0.0
	for _, part := range []struct {
		count int64
		price *float64
		kind  string
	}{
		{u.InputTokens, &band.Input, "input"}, {u.OutputTokens, &band.Output, "output"},
		{u.CacheWriteTokens, band.CacheWrite, "cache_write"}, {u.CacheReadTokens, band.CacheRead, "cache_read"},
	} {
		if part.count != 0 {
			if part.price == nil {
				return 0, &UnknownBillError{fmt.Sprintf("model %s has no %s price in its band", route.PyRepr(model), part.kind)}
			}
			usd += float64(part.count) * *part.price
		}
	}
	return usd / 1_000_000, nil
}
