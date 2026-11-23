// Package factorycheckpoint saves the state needed to resume specification work
// after an authorized product decision.
package factorycheckpoint

import (
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"os"
	"regexp"
	"strings"
	"time"
	"unicode/utf8"
)

var (
	ErrInvalid  = errors.New("invalid checkpoint input")
	ErrNotFound = errors.New("no checkpoint for job")
	ErrStale    = errors.New("checkpoint is stale")
	ErrConflict = errors.New("checkpoint already has a different answer")
	ErrPending  = errors.New("checkpoint is waiting for an answer")
	ErrCorrupt  = errors.New("stored checkpoint is invalid")
)

var (
	jobIDPattern  = regexp.MustCompile(`^[0-9]+-[a-z0-9]+(?:-[a-z0-9]+)*$`)
	digestPattern = regexp.MustCompile(`^[0-9a-f]{64}$`)
	basePattern   = regexp.MustCompile(`^(?:[0-9a-f]{40}|[0-9a-f]{64})$`)
)

// Binding identifies the exact specification inputs observed by the caller.
// DraftDigest is the lowercase SHA-256 of the original draft bytes. Base is a
// full Git object ID.
type Binding struct {
	JobID        string `json:"job_id"`
	SpecRevision string `json:"spec_revision"`
	DraftDigest  string `json:"draft_sha256"`
	Base         string `json:"base"`
}

// DraftDigest returns the lowercase SHA-256 of the exact draft bytes.
func DraftDigest(draft []byte) string {
	digest := sha256.Sum256(draft)
	return hex.EncodeToString(digest[:])
}

type Question struct {
	ID     string `json:"id"`
	Prompt string `json:"prompt"`
}

type PauseRequest struct {
	Binding       Binding  `json:"binding"`
	Draft         []byte   `json:"draft"`
	Question      Question `json:"question"`
	ResumePoint   string   `json:"resume_point"`
	CompletedWork []string `json:"completed_work"`
	Evidence      []string `json:"evidence"`
}

// Answer records one decision and its provenance. ActorID and SourceRef must
// come from an authorized source verified by the caller.
type Answer struct {
	QuestionID string `json:"question_id"`
	Text       string `json:"text"`
	ActorID    string `json:"actor_id"`
	SourceRef  string `json:"source_ref"`
}

type SavedAnswer struct {
	Answer
	RecordedAt time.Time `json:"recorded_at"`
}

type answerRecord struct {
	CheckpointRef string `json:"checkpoint_ref"`
	SavedAnswer
}

type Checkpoint struct {
	Ref         string `json:"ref"`
	PreviousRef string `json:"previous_ref,omitempty"`
	PauseRequest
	CreatedAt time.Time    `json:"created_at"`
	Answer    *SavedAnswer `json:"answer,omitempty"`
}

// Pause persists or supersedes a job's checkpoint. previousRef is the active
// checkpoint the caller observed, or empty for the first pause. Retrying the
// same publication returns the existing checkpoint, including any later answer.
func (s *Store) Pause(request PauseRequest, previousRef string) (Checkpoint, error) {
	if !validPause(request) || (previousRef != "" && !digestPattern.MatchString(previousRef)) {
		return Checkpoint{}, ErrInvalid
	}
	ref := checkpointRef(request, previousRef)

	directory, err := s.lock()
	if err != nil {
		return Checkpoint{}, err
	}
	defer directory.Close()

	current, err := s.readCurrent(request.Binding.JobID)
	if err != nil && !errors.Is(err, ErrNotFound) {
		return Checkpoint{}, err
	}
	if current.Ref == ref {
		return current, directory.Sync()
	}
	if current.Ref != previousRef {
		return Checkpoint{}, ErrStale
	}

	record, err := s.readCheckpoint(request.Binding.JobID, ref)
	switch {
	case errors.Is(err, os.ErrNotExist):
		record = Checkpoint{
			Ref:          ref,
			PreviousRef:  previousRef,
			PauseRequest: request,
			CreatedAt:    time.Now().UTC(),
		}
		if err := writeJSON(directory, checkpointName(ref), record); err != nil {
			return Checkpoint{}, err
		}
	case err != nil:
		return Checkpoint{}, err
	case record.Answer != nil:
		return Checkpoint{}, ErrCorrupt
	}

	// Publish the pointer only after the draft is durable. A failed publication
	// leaves the old pointer readable and the next retry can reuse the draft.
	if err := writeJSON(directory, currentName(request.Binding.JobID), ref); err != nil {
		return Checkpoint{}, err
	}
	return record, nil
}

// SaveAnswer accepts one answer for the current checkpoint. Identical retries
// return the original provenance and timestamp. The caller supplies fresh job
// and specification inputs independently of the submitted answer.
func (s *Store) SaveAnswer(ref string, observed Binding, answer Answer) (Checkpoint, error) {
	if !validAnswer(answer) {
		return Checkpoint{}, ErrInvalid
	}

	directory, err := s.lock()
	if err != nil {
		return Checkpoint{}, err
	}
	defer directory.Close()

	record, err := s.matchCurrent(ref, observed)
	if err != nil {
		return Checkpoint{}, err
	}
	if record.Question.ID != answer.QuestionID {
		return Checkpoint{}, ErrStale
	}
	if record.Answer != nil {
		if record.Answer.Answer != answer {
			return Checkpoint{}, ErrConflict
		}
		return record, directory.Sync()
	}
	saved := SavedAnswer{Answer: answer, RecordedAt: time.Now().UTC()}
	if err := writeJSON(directory, answerName(ref), answerRecord{CheckpointRef: ref, SavedAnswer: saved}); err != nil {
		return Checkpoint{}, err
	}
	record.Answer = &saved
	return record, nil
}

// Resume returns the saved answer, exact draft, and resume point after checking
// the current binding. A controller can use Ref as the stable identity of a
// resume transition. This method leaves the checkpoint available for retries.
func (s *Store) Resume(ref string, observed Binding) (Checkpoint, error) {
	directory, err := s.lock()
	if err != nil {
		return Checkpoint{}, err
	}
	defer directory.Close()

	record, err := s.matchCurrent(ref, observed)
	if err != nil {
		return Checkpoint{}, err
	}
	if record.Answer == nil {
		return Checkpoint{}, ErrPending
	}
	return record, directory.Sync()
}

// Current reads the job's active checkpoint.
func (s *Store) Current(jobID string) (Checkpoint, error) {
	if !validJobID(jobID) {
		return Checkpoint{}, ErrInvalid
	}
	directory, err := s.lock()
	if err != nil {
		return Checkpoint{}, err
	}
	defer directory.Close()

	return s.readCurrent(jobID)
}

// History reads the current checkpoint and its predecessors, newest first.
func (s *Store) History(jobID string) ([]Checkpoint, error) {
	if !validJobID(jobID) {
		return nil, ErrInvalid
	}
	directory, err := s.lock()
	if err != nil {
		return nil, err
	}
	defer directory.Close()

	record, err := s.readCurrent(jobID)
	if err != nil {
		return nil, err
	}
	history := []Checkpoint{record}
	for record.PreviousRef != "" {
		record, err = s.readCheckpoint(jobID, record.PreviousRef)
		if err != nil {
			return nil, fmt.Errorf("%w: read predecessor: %w", ErrCorrupt, err)
		}
		history = append(history, record)
	}
	return history, nil
}

func (s *Store) matchCurrent(ref string, observed Binding) (Checkpoint, error) {
	if !digestPattern.MatchString(ref) || !validBinding(observed) {
		return Checkpoint{}, ErrInvalid
	}
	record, err := s.readCurrent(observed.JobID)
	if errors.Is(err, ErrNotFound) {
		return Checkpoint{}, ErrStale
	}
	if err != nil {
		return Checkpoint{}, err
	}
	if record.Ref != ref || record.Binding != observed {
		return Checkpoint{}, ErrStale
	}
	return record, nil
}

func (s *Store) readCurrent(jobID string) (Checkpoint, error) {
	ref, err := readJSON[string](s.dir, currentName(jobID))
	if errors.Is(err, os.ErrNotExist) {
		return Checkpoint{}, ErrNotFound
	}
	if err != nil || !digestPattern.MatchString(ref) {
		return Checkpoint{}, fmt.Errorf("%w: invalid current pointer for job %s", ErrCorrupt, jobID)
	}
	record, err := s.readCheckpoint(jobID, ref)
	if err != nil {
		return Checkpoint{}, fmt.Errorf("%w: read current checkpoint: %w", ErrCorrupt, err)
	}
	return record, nil
}

func (s *Store) readCheckpoint(jobID, ref string) (Checkpoint, error) {
	record, err := readJSON[Checkpoint](s.dir, checkpointName(ref))
	if err != nil {
		return Checkpoint{}, err
	}
	if record.Binding.JobID != jobID || record.Ref != ref || !validPause(record.PauseRequest) ||
		record.CreatedAt.IsZero() || (record.PreviousRef != "" && !digestPattern.MatchString(record.PreviousRef)) ||
		checkpointRef(record.PauseRequest, record.PreviousRef) != ref || record.Answer != nil {
		return Checkpoint{}, ErrCorrupt
	}
	answer, err := readJSON[answerRecord](s.dir, answerName(ref))
	if errors.Is(err, os.ErrNotExist) {
		return record, nil
	}
	if err != nil {
		return Checkpoint{}, fmt.Errorf("%w: read answer: %w", ErrCorrupt, err)
	}
	if answer.CheckpointRef != ref || !validAnswer(answer.Answer) ||
		answer.RecordedAt.IsZero() || answer.QuestionID != record.Question.ID {
		return Checkpoint{}, ErrCorrupt
	}
	record.Answer = &answer.SavedAnswer
	return record, nil
}

func checkpointRef(request PauseRequest, previousRef string) string {
	// The identity contains only strings, a byte slice and slices of strings.
	// JSON encoding is deterministic and cannot fail for these fields.
	data, _ := json.Marshal(struct {
		PreviousRef string `json:"previous_ref"`
		PauseRequest
	}{PreviousRef: previousRef, PauseRequest: request})
	return DraftDigest(data)
}

func currentName(jobID string) string {
	return "job-" + DraftDigest([]byte(jobID)) + ".json"
}

func checkpointName(ref string) string {
	return "checkpoint-" + ref + ".json"
}

func answerName(ref string) string {
	return "answer-" + ref + ".json"
}

func validJobID(jobID string) bool {
	return len(jobID) <= 255 && jobIDPattern.MatchString(jobID)
}

func validBinding(binding Binding) bool {
	return validJobID(binding.JobID) && present(binding.SpecRevision) &&
		digestPattern.MatchString(binding.DraftDigest) && basePattern.MatchString(binding.Base)
}

func validPause(request PauseRequest) bool {
	if !validBinding(request.Binding) || len(request.Draft) == 0 ||
		DraftDigest(request.Draft) != request.Binding.DraftDigest ||
		!present(request.Question.ID) || !present(request.Question.Prompt) || !present(request.ResumePoint) {
		return false
	}
	for _, refs := range [][]string{request.CompletedWork, request.Evidence} {
		for _, ref := range refs {
			if !present(ref) {
				return false
			}
		}
	}
	return true
}

func validAnswer(answer Answer) bool {
	return present(answer.QuestionID) && present(answer.Text) && present(answer.ActorID) && present(answer.SourceRef)
}

func present(text string) bool {
	return utf8.ValidString(text) && strings.TrimSpace(text) != ""
}
