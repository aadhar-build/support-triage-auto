// Command factory-autonomy-shadow validates one proposed action without dispatching work.
package main

import (
	"bytes"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"os"
	"strings"

	"github.com/aadhar-build/support-triage-auto/backend/internal/factorycontroller"
)

type request struct {
	Snapshot factorycontroller.Snapshot `json:"snapshot"`
	Proposal factorycontroller.Proposal `json:"proposal"`
}

type response struct {
	Mode               string `json:"mode"`
	DispatchAuthorized bool   `json:"dispatch_authorized"`
	Accepted           bool   `json:"accepted"`
	Action             string `json:"action"`
	Reason             string `json:"reason,omitempty"`
}

func reasonCode(err error) string {
	switch {
	case errors.Is(err, factorycontroller.ErrStale):
		return "stale-revision"
	case errors.Is(err, factorycontroller.ErrWrongStage):
		return "wrong-stage"
	case errors.Is(err, factorycontroller.ErrOwnerAuthorization):
		return "owner-authorization-required"
	case errors.Is(err, factorycontroller.ErrPrerequisites):
		return "prerequisites-incomplete"
	case errors.Is(err, factorycontroller.ErrNoCapacity):
		return "no-build-capacity"
	case errors.Is(err, factorycontroller.ErrInvalidSnapshot):
		return "invalid-snapshot"
	default:
		return "invalid-proposal"
	}
}

// Validate keys while tokenizing, before duplicate names can be discarded.
func validateKeys(dec *json.Decoder, depth int) error {
	if depth > 128 {
		return errors.New("request nesting exceeds limit")
	}
	tok, err := dec.Token()
	if err != nil {
		return err
	}
	delim, isDelim := tok.(json.Delim)
	if !isDelim {
		return nil
	}
	switch delim {
	case '{':
		seen := make(map[string]bool)
		for dec.More() {
			keyToken, err := dec.Token()
			if err != nil {
				return err
			}
			key, ok := keyToken.(string)
			if !ok {
				return errors.New("invalid object key")
			}
			for _, ch := range key {
				if ch > 127 {
					return errors.New("request contains non-ASCII field name")
				}
			}
			folded := strings.ToLower(key)
			if seen[folded] {
				return errors.New("request contains ambiguous field names")
			}
			seen[folded] = true
			if err := validateKeys(dec, depth+1); err != nil {
				return err
			}
		}
		_, err = dec.Token()
		return err
	case '[':
		for dec.More() {
			if err := validateKeys(dec, depth+1); err != nil {
				return err
			}
		}
		_, err = dec.Token()
		return err
	default:
		return errors.New("invalid JSON delimiter")
	}
}

// run is intentionally read-only. The supplied snapshot is only as trustworthy
// as its caller; this command must not be used as an admission authorization.
func run(input io.Reader, output io.Writer) error {
	const maxInput = 1 << 20
	raw, err := io.ReadAll(io.LimitReader(input, maxInput+1))
	if err != nil {
		return fmt.Errorf("read request: %w", err)
	}
	if len(raw) > maxInput {
		return errors.New("request exceeds size limit")
	}
	if err := validateKeys(json.NewDecoder(bytes.NewReader(raw)), 0); err != nil {
		return fmt.Errorf("decode request: %w", err)
	}
	var fields struct {
		Snapshot map[string]json.RawMessage `json:"snapshot"`
		Proposal map[string]json.RawMessage `json:"proposal"`
	}
	if err := json.Unmarshal(raw, &fields); err != nil {
		return fmt.Errorf("decode request fields: %w", err)
	}
	var action string
	if err := json.Unmarshal(fields.Proposal["Action"], &action); err != nil {
		return errors.New("proposal Action is required")
	}
	if action == string(factorycontroller.ActionPickupBuild) {
		for _, key := range []string{"BuildCapacity", "ActiveBuilds", "Jobs"} {
			value, ok := fields.Snapshot[key]
			if !ok || bytes.Equal(bytes.TrimSpace(value), []byte("null")) {
				return fmt.Errorf("snapshot %s is required", key)
			}
		}
		var jobs []map[string]json.RawMessage
		if err := json.Unmarshal(fields.Snapshot["Jobs"], &jobs); err != nil {
			return errors.New("snapshot Jobs must be an array")
		}
		for _, job := range jobs {
			value, ok := job["Round"]
			if !ok || bytes.Equal(bytes.TrimSpace(value), []byte("null")) {
				return errors.New("each job Round is required")
			}
		}
	}
	dec := json.NewDecoder(bytes.NewReader(raw))
	dec.DisallowUnknownFields()
	var req request
	if err := dec.Decode(&req); err != nil {
		return fmt.Errorf("decode request: %w", err)
	}
	var extra any
	if err := dec.Decode(&extra); !errors.Is(err, io.EOF) {
		return errors.New("request must contain exactly one JSON object")
	}
	_, err = factorycontroller.Validate(req.Snapshot, req.Proposal)
	result := response{Mode: "supplied-snapshot-shadow", Accepted: err == nil, Action: "unsupported"}
	if req.Proposal.Action == factorycontroller.ActionNoAction || req.Proposal.Action == factorycontroller.ActionPickupBuild {
		result.Action = string(req.Proposal.Action)
	}
	if err != nil {
		result.Reason = reasonCode(err)
	}
	return json.NewEncoder(output).Encode(result)
}

func main() {
	if err := run(os.Stdin, os.Stdout); err != nil {
		fmt.Fprintln(os.Stderr, "Invalid shadow request.")
		os.Exit(2)
	}
}
