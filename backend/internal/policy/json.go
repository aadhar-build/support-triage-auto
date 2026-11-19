package policy

import (
	"bytes"
	"encoding/json"
	"errors"
	"fmt"
	"io"
)

// Kind is the JSON type of a Value.
type Kind int

// The JSON kinds a Value can hold.
const (
	Null Kind = iota
	Bool
	Number
	String
	Array
	Object
)

// Value is a decoded JSON value that keeps object key order the way a Python
// dict built by json.loads does: a duplicate key keeps its first position and
// takes the last value. Numbers keep their literal text.
type Value struct {
	Kind  Kind
	Bool  bool
	Num   string // literal text, for Number
	Str   string
	Items []Value  // Array
	Keys  []string // Object, in first-seen order
	Vals  map[string]Value
}

// Get returns the value of an object key (Python dict.get); ok is false when
// v is not an object or has no such key.
func (v Value) Get(key string) (Value, bool) {
	if v.Kind != Object {
		return Value{}, false
	}
	x, ok := v.Vals[key]
	return x, ok
}

// IsInt reports whether a Number literal is a JSON int (Python int).
func (v Value) IsInt() bool {
	return v.Kind == Number && !bytes.ContainsAny([]byte(v.Num), ".eE")
}

// ParseJSON decodes one JSON value followed only by JSON whitespace. It refuses
// NaN, Infinity and unpaired surrogate escapes, which Python's json accepts.
func ParseJSON(b []byte) (Value, error) {
	if err := checkSurrogates(b); err != nil {
		return Value{}, err
	}
	dec := json.NewDecoder(bytes.NewReader(b))
	dec.UseNumber()
	v, err := decodeValue(dec)
	if err != nil {
		if errors.Is(err, io.EOF) {
			return Value{}, errors.New("unexpected end of JSON input")
		}
		return Value{}, err
	}
	for i, c := range b[dec.InputOffset():] {
		if c != ' ' && c != '\t' && c != '\n' && c != '\r' {
			return Value{}, fmt.Errorf("extra data at offset %d", int(dec.InputOffset())+i)
		}
	}
	return v, nil
}

func decodeValue(dec *json.Decoder) (Value, error) {
	tok, err := dec.Token()
	if err != nil {
		return Value{}, err
	}
	switch t := tok.(type) {
	case nil:
		return Value{Kind: Null}, nil
	case bool:
		return Value{Kind: Bool, Bool: t}, nil
	case json.Number:
		return Value{Kind: Number, Num: string(t)}, nil
	case string:
		return Value{Kind: String, Str: t}, nil
	case json.Delim:
		switch t {
		case '[':
			v := Value{Kind: Array, Items: []Value{}}
			for dec.More() {
				item, err := decodeValue(dec)
				if err != nil {
					return Value{}, err
				}
				v.Items = append(v.Items, item)
			}
			if _, err := dec.Token(); err != nil {
				return Value{}, err
			}
			return v, nil
		case '{':
			v := Value{Kind: Object, Keys: []string{}, Vals: map[string]Value{}}
			for dec.More() {
				kt, err := dec.Token()
				if err != nil {
					return Value{}, err
				}
				key, ok := kt.(string)
				if !ok {
					return Value{}, fmt.Errorf("object key %v is not a string", kt)
				}
				item, err := decodeValue(dec)
				if err != nil {
					return Value{}, err
				}
				if _, seen := v.Vals[key]; !seen {
					v.Keys = append(v.Keys, key)
				}
				v.Vals[key] = item
			}
			if _, err := dec.Token(); err != nil {
				return Value{}, err
			}
			return v, nil
		}
	}
	return Value{}, fmt.Errorf("unexpected token %v", tok)
}

// checkSurrogates refuses a \uD800-\uDFFF escape that is not a high surrogate
// immediately followed by a low one. encoding/json would silently turn it into
// U+FFFD. Malformed escapes are left for the decoder to report.
func checkSurrogates(b []byte) error {
	inStr := false
	for i := 0; i < len(b); i++ {
		c := b[i]
		if !inStr {
			if c == '"' {
				inStr = true
			}
			continue
		}
		switch c {
		case '"':
			inStr = false
		case '\\':
			if i+1 < len(b) && b[i+1] == 'u' {
				u, ok := hex4(b, i+2)
				if !ok {
					return nil
				}
				switch {
				case u >= 0xD800 && u < 0xDC00:
					lo, ok := -1, false
					if i+7 < len(b) && b[i+6] == '\\' && b[i+7] == 'u' {
						lo, ok = hex4(b, i+8)
					}
					if !ok || lo < 0xDC00 || lo > 0xDFFF {
						return fmt.Errorf("unpaired surrogate escape at offset %d", i)
					}
					i += 11
				case u >= 0xDC00 && u <= 0xDFFF:
					return fmt.Errorf("unpaired surrogate escape at offset %d", i)
				default:
					i += 5
				}
			} else {
				i++
			}
		}
	}
	return nil
}

func hex4(b []byte, at int) (int, bool) {
	if at+4 > len(b) {
		return 0, false
	}
	n := 0
	for _, c := range b[at : at+4] {
		switch {
		case c >= '0' && c <= '9':
			n = n*16 + int(c-'0')
		case c >= 'a' && c <= 'f':
			n = n*16 + int(c-'a') + 10
		case c >= 'A' && c <= 'F':
			n = n*16 + int(c-'A') + 10
		default:
			return 0, false
		}
	}
	return n, true
}
