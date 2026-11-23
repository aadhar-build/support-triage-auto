package main

import (
	"bytes"
	"encoding/json"
	"strings"
	"testing"
)

func TestReadOnlyDecision(t *testing.T) {
	input := `{"snapshot":{"Revision":"r1"},"proposal":{"Action":"no-action","StateRevision":"r1"}}`
	var out bytes.Buffer
	if err := run(strings.NewReader(input), &out); err != nil {
		t.Fatal(err)
	}
	var got response
	if err := json.Unmarshal(out.Bytes(), &got); err != nil || !got.Accepted {
		t.Fatalf("response=%s err=%v", out.String(), err)
	}
}

func TestRejectsStaleProposal(t *testing.T) {
	input := `{"snapshot":{"Revision":"r2"},"proposal":{"Action":"no-action","StateRevision":"r1"}}`
	var out bytes.Buffer
	if err := run(strings.NewReader(input), &out); err != nil {
		t.Fatal(err)
	}
	var got response
	if err := json.Unmarshal(out.Bytes(), &got); err != nil || got.Accepted || got.Reason == "" {
		t.Fatalf("response=%s err=%v", out.String(), err)
	}
}

func TestRejectsUnexpectedInput(t *testing.T) {
	for _, input := range []string{`{"snapshot":{},"proposal":{},"unexpected":true}`, `{} {}`} {
		if err := run(strings.NewReader(input), &bytes.Buffer{}); err == nil {
			t.Fatalf("accepted %q", input)
		}
	}
}

func TestRejectsDataBeyondLimit(t *testing.T) {
	input := `{"snapshot":{"Revision":"r1"},"proposal":{"Action":"no-action","StateRevision":"r1"}}`
	input += strings.Repeat(" ", (1<<20)-len(input)) + `{}`
	if err := run(strings.NewReader(input), &bytes.Buffer{}); err == nil {
		t.Fatal("accepted oversized request")
	}
}

func TestPickupRequiresCapacityAndJobRound(t *testing.T) {
	for _, input := range []string{
		`{"snapshot":{"Revision":"r1","BuildCapacity":1,"Jobs":[]},"proposal":{"Action":"pickup-build","StateRevision":"r1","JobID":"j","JobRevision":"1"}}`,
		`{"snapshot":{"Revision":"r1","BuildCapacity":1,"ActiveBuilds":null,"Jobs":[]},"proposal":{"Action":"pickup-build","StateRevision":"r1","JobID":"j","JobRevision":"1"}}`,
		`{"snapshot":{"Revision":"r1","BuildCapacity":1,"ActiveBuilds":0,"Jobs":[{"ID":"j"}]},"proposal":{"Action":"pickup-build","StateRevision":"r1","JobID":"j","JobRevision":"1"}}`,
	} {
		if err := run(strings.NewReader(input), &bytes.Buffer{}); err == nil {
			t.Fatalf("accepted incomplete request %q", input)
		}
	}
}

func TestRejectsAmbiguousFieldCasing(t *testing.T) {
	input := `{"snapshot":{"Revision":"r1","BuildCapacity":1,"ActiveBuilds":0,"Jobs":[],"jobs":[{"ID":"j"}]},"proposal":{"Action":"no-action","action":"pickup-build","StateRevision":"r1"}}`
	if err := run(strings.NewReader(input), &bytes.Buffer{}); err == nil {
		t.Fatal("accepted ambiguous field names")
	}
}

func TestRejectsDuplicateActionIncludingNull(t *testing.T) {
	input := `{"snapshot":{"Revision":"r1"},"proposal":{"Action":"pickup-build","Action":null,"StateRevision":"r1"}}`
	if err := run(strings.NewReader(input), &bytes.Buffer{}); err == nil {
		t.Fatal("accepted duplicate action")
	}
}

func TestShadowResponseCannotBeMistakenForDispatchAuthorization(t *testing.T) {
	input := `{"snapshot":{"Revision":"r1"},"proposal":{"Action":"private-unsupported-value","StateRevision":"r1"}}`
	var out bytes.Buffer
	if err := run(strings.NewReader(input), &out); err != nil {
		t.Fatal(err)
	}
	var got response
	if err := json.Unmarshal(out.Bytes(), &got); err != nil {
		t.Fatal(err)
	}
	if got.Mode != "supplied-snapshot-shadow" || got.DispatchAuthorized || got.Accepted || got.Action != "unsupported" || strings.Contains(out.String(), "private-unsupported-value") {
		t.Fatalf("unsafe response: %s", out.String())
	}
}
