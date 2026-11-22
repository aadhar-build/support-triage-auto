// Package factorystage records controller stage events in append order.
package factorystage

import (
	"errors"
	"fmt"
	"strings"
	"time"
	"unicode"
	"unicode/utf8"
)

const (
	maxIdentifierBytes = 256
	maxEvidenceBytes   = 4096
	maxReasonBytes     = 512
)

var (
	// ErrInvalidEvent indicates that a supplied event failed validation.
	ErrInvalidEvent = errors.New("invalid stage event")
	// ErrConflict indicates that an event ID already identifies different data.
	ErrConflict = errors.New("conflicting stage event ID")
	// ErrCorrupt indicates that a published log cannot be safely replayed.
	ErrCorrupt = errors.New("corrupt stage event log")
)

// Event is a replayable stage record. The caller assigns the stable EventID
// and supplies OccurredAt when the event occurs. A retry must reuse both.
// Round is zero before the first build round.
// Status and Outcome are controller labels. EvidenceRef is an opaque reference.
// Reason must be a short, privacy-safe summary.
type Event struct {
	EventID             string    `json:"event_id"`
	JobID               string    `json:"job_id"`
	Round               int       `json:"round"`
	AttemptID           string    `json:"attempt_id"`
	JobRevision         string    `json:"job_revision"`
	Stage               string    `json:"stage"`
	Status              string    `json:"status"`
	Outcome             string    `json:"outcome"`
	OccurredAt          time.Time `json:"occurred_at"`
	EvidenceRef         string    `json:"evidence_ref"`
	Provider            string    `json:"provider,omitempty"`
	Model               string    `json:"model,omitempty"`
	Reason              string    `json:"reason,omitempty"`
	OperationID         string    `json:"operation_id,omitempty"`
	SupersedesAttemptID string    `json:"supersedes_attempt_id,omitempty"`
}

func (e Event) validate() error {
	for _, field := range []struct{ name, value string }{
		{"event_id", e.EventID}, {"job_id", e.JobID}, {"attempt_id", e.AttemptID},
		{"job_revision", e.JobRevision}, {"stage", e.Stage}, {"status", e.Status},
		{"outcome", e.Outcome},
	} {
		if !validIdentifier(field.value) {
			return fmt.Errorf("%w: invalid or missing %s", ErrInvalidEvent, field.name)
		}
	}
	for _, field := range []struct{ name, value string }{
		{"provider", e.Provider}, {"model", e.Model}, {"operation_id", e.OperationID},
		{"supersedes_attempt_id", e.SupersedesAttemptID},
	} {
		if field.value != "" && !validIdentifier(field.value) {
			return fmt.Errorf("%w: invalid %s", ErrInvalidEvent, field.name)
		}
	}
	if e.Round < 0 {
		return fmt.Errorf("%w: round cannot be negative", ErrInvalidEvent)
	}
	if e.SupersedesAttemptID == e.AttemptID {
		return fmt.Errorf("%w: attempt cannot supersede itself", ErrInvalidEvent)
	}
	if e.OccurredAt.IsZero() || e.OccurredAt.Year() < 1 || e.OccurredAt.Year() > 9999 {
		return fmt.Errorf("%w: invalid or missing occurred_at", ErrInvalidEvent)
	}
	if !validText(e.EvidenceRef, maxEvidenceBytes) {
		return fmt.Errorf("%w: invalid or missing evidence_ref", ErrInvalidEvent)
	}
	if e.Reason != "" && !validText(e.Reason, maxReasonBytes) {
		return fmt.Errorf("%w: invalid reason", ErrInvalidEvent)
	}
	return nil
}

func validIdentifier(value string) bool {
	return validText(value, maxIdentifierBytes) && strings.IndexFunc(value, unicode.IsSpace) < 0
}

func validText(value string, maxBytes int) bool {
	if value == "" || len(value) > maxBytes || !utf8.ValidString(value) || strings.TrimSpace(value) != value {
		return false
	}
	return strings.IndexFunc(value, func(r rune) bool {
		return unicode.IsControl(r) || r == '\u2028' || r == '\u2029'
	}) < 0
}
