package factorycheckpoint

import (
	"errors"
	"os"
	"path/filepath"
	"reflect"
	"strings"
	"sync"
	"testing"
	"time"
)

func testPause(jobID string) PauseRequest {
	draft := []byte("# Draft\r\n\r\nResume from this exact text.\n")
	return PauseRequest{
		Binding: Binding{
			JobID: jobID, SpecRevision: "rev-1", DraftDigest: DraftDigest(draft),
			Base: strings.Repeat("a", 40),
		},
		Draft:         draft,
		Question:      Question{ID: "limit", Prompt: "Which export limit should we apply?"},
		ResumePoint:   "validate revised requirements",
		CompletedWork: []string{"spec:required-sections"},
		Evidence:      []string{"evidence:scope-v1", "evidence:intake-42"},
	}
}

func testAnswer() Answer {
	return Answer{QuestionID: "limit", Text: "Use 25 rows.\n", ActorID: "owner-42", SourceRef: "issue-comment:1001"}
}

func openTestStore(t *testing.T, dir string) *Store {
	t.Helper()
	store, err := Open(dir)
	if err != nil {
		t.Fatal(err)
	}
	return store
}

func pauseTestJob(t *testing.T, store *Store, request PauseRequest, previous string) Checkpoint {
	t.Helper()
	checkpoint, err := store.Pause(request, previous)
	if err != nil {
		t.Fatal(err)
	}
	return checkpoint
}

func TestCheckpointResumesFromSavedDraftAndAnswer(t *testing.T) {
	dir := t.TempDir()
	request := testPause("042-checkpoint")
	first := openTestStore(t, dir)
	paused := pauseTestJob(t, first, request, "")
	if paused.Ref == "" || paused.Answer != nil {
		t.Fatalf("invalid pending checkpoint: %+v", paused)
	}
	if _, err := first.Resume(paused.Ref, request.Binding); !errors.Is(err, ErrPending) {
		t.Fatalf("resume without an answer = %v, want ErrPending", err)
	}

	afterPause := openTestStore(t, dir)
	answer := testAnswer()
	answered, err := afterPause.SaveAnswer(paused.Ref, request.Binding, answer)
	if err != nil {
		t.Fatal(err)
	}
	if answered.Answer == nil || answered.Answer.Answer != answer || answered.Answer.RecordedAt.IsZero() {
		t.Fatalf("answer provenance not recorded: %+v", answered)
	}

	afterAnswer := openTestStore(t, dir)
	resumed, err := afterAnswer.Resume(paused.Ref, request.Binding)
	if err != nil {
		t.Fatal(err)
	}
	if !reflect.DeepEqual(resumed.PauseRequest, request) || !reflect.DeepEqual(resumed, answered) {
		t.Fatalf("recovered checkpoint differs: got %+v, want %+v", resumed, answered)
	}
	again, err := afterAnswer.Resume(paused.Ref, request.Binding)
	if err != nil || !reflect.DeepEqual(again, resumed) {
		t.Fatalf("resume retry changed the checkpoint identity or data: %+v, %v", again, err)
	}
}

func TestCheckpointRejectsMismatchedInputsAndConflictingAnswers(t *testing.T) {
	store := openTestStore(t, t.TempDir())
	request := testPause("042-checkpoint")
	paused := pauseTestJob(t, store, request, "")
	answer := testAnswer()

	for _, tc := range []struct {
		name string
		edit func(*Binding)
	}{
		{"job", func(b *Binding) { b.JobID = "043-other" }},
		{"revision", func(b *Binding) { b.SpecRevision = "rev-2" }},
		{"digest", func(b *Binding) { b.DraftDigest = DraftDigest([]byte("another draft")) }},
		{"base", func(b *Binding) { b.Base = strings.Repeat("b", 40) }},
	} {
		t.Run(tc.name, func(t *testing.T) {
			observed := request.Binding
			tc.edit(&observed)
			if _, err := store.SaveAnswer(paused.Ref, observed, answer); !errors.Is(err, ErrStale) {
				t.Fatalf("answer against mismatched %s = %v, want ErrStale", tc.name, err)
			}
			if _, err := store.Resume(paused.Ref, observed); !errors.Is(err, ErrStale) {
				t.Fatalf("resume against mismatched %s = %v, want ErrStale", tc.name, err)
			}
		})
	}
	mismatchedQuestion := answer
	mismatchedQuestion.QuestionID = "not-the-question"
	if _, err := store.SaveAnswer(paused.Ref, request.Binding, mismatchedQuestion); !errors.Is(err, ErrStale) {
		t.Fatalf("answer for another question = %v, want ErrStale", err)
	}

	noActor := answer
	noActor.ActorID = ""
	if _, err := store.SaveAnswer(paused.Ref, request.Binding, noActor); !errors.Is(err, ErrInvalid) {
		t.Fatalf("answer without an actor = %v, want ErrInvalid", err)
	}
	noSource := answer
	noSource.SourceRef = ""
	if _, err := store.SaveAnswer(paused.Ref, request.Binding, noSource); !errors.Is(err, ErrInvalid) {
		t.Fatalf("answer without a source = %v, want ErrInvalid", err)
	}
	recorded, err := store.SaveAnswer(paused.Ref, request.Binding, answer)
	if err != nil {
		t.Fatal(err)
	}
	duplicate, err := store.SaveAnswer(paused.Ref, request.Binding, answer)
	if err != nil || !reflect.DeepEqual(recorded, duplicate) {
		t.Fatalf("duplicate answer changed the record: %+v, %v", duplicate, err)
	}

	for _, tc := range []struct {
		name string
		edit func(*Answer)
	}{
		{"text", func(a *Answer) { a.Text = "Use 50 rows." }},
		{"actor", func(a *Answer) { a.ActorID = "owner-99" }},
		{"source", func(a *Answer) { a.SourceRef = "issue-comment:1002" }},
	} {
		t.Run(tc.name, func(t *testing.T) {
			other := answer
			tc.edit(&other)
			if _, err := store.SaveAnswer(paused.Ref, request.Binding, other); !errors.Is(err, ErrConflict) {
				t.Fatalf("conflicting answer %s = %v, want ErrConflict", tc.name, err)
			}
		})
	}
	current, err := store.Current(request.Binding.JobID)
	if err != nil || !reflect.DeepEqual(recorded, current) {
		t.Fatalf("rejected writes changed the saved answer: %+v, %v", current, err)
	}
}

func TestCheckpointRevisionHistoryRejectsSupersededAnswers(t *testing.T) {
	dir := t.TempDir()
	store := openTestStore(t, dir)
	first := testPause("042-checkpoint")
	prior := pauseTestJob(t, store, first, "")
	answer := testAnswer()
	prior, err := store.SaveAnswer(prior.Ref, first.Binding, answer)
	if err != nil {
		t.Fatal(err)
	}
	next := first
	next.Binding.SpecRevision = "rev-2"
	next.Draft = []byte("# Revised draft\n")
	next.Binding.DraftDigest = DraftDigest(next.Draft)
	next.Question = Question{ID: "format", Prompt: "Should we export CSV or JSON?"}

	if _, err := store.Pause(next, ""); !errors.Is(err, ErrStale) {
		t.Fatalf("pause based on missing prior checkpoint = %v, want ErrStale", err)
	}
	current := pauseTestJob(t, store, next, prior.Ref)
	if current.Ref == prior.Ref || current.PreviousRef != prior.Ref {
		t.Fatalf("revision lineage missing: %+v", current)
	}

	restarted := openTestStore(t, dir)
	retry := pauseTestJob(t, restarted, next, prior.Ref)
	if !reflect.DeepEqual(retry, current) {
		t.Fatalf("pause retry changed the active checkpoint: %+v", retry)
	}
	if _, err := restarted.SaveAnswer(prior.Ref, first.Binding, answer); !errors.Is(err, ErrStale) {
		t.Fatalf("superseded answer = %v, want ErrStale", err)
	}
	if _, err := restarted.Pause(first, ""); !errors.Is(err, ErrStale) {
		t.Fatalf("superseded pause = %v, want ErrStale", err)
	}
	history, err := restarted.History(first.Binding.JobID)
	if err != nil || !reflect.DeepEqual(history, []Checkpoint{current, prior}) {
		t.Fatalf("history did not preserve the draft and answer: %+v, %v", history, err)
	}
}

func TestCheckpointConcurrentAnswersCannotOverwriteEachOther(t *testing.T) {
	dir := t.TempDir()
	first := openTestStore(t, dir)
	second := openTestStore(t, dir)
	request := testPause("042-checkpoint")
	paused := pauseTestJob(t, first, request, "")
	answer := testAnswer()
	other := Answer{QuestionID: "limit", Text: "Use 50 rows.", ActorID: "owner-42", SourceRef: "issue-comment:1002"}

	type result struct {
		checkpoint Checkpoint
		err        error
	}
	ready := make(chan struct{})
	results := make(chan result, 2)
	var group sync.WaitGroup
	for i, store := range []*Store{first, second} {
		candidate := []Answer{answer, other}[i]
		group.Add(1)
		go func() {
			defer group.Done()
			<-ready
			saved, err := store.SaveAnswer(paused.Ref, request.Binding, candidate)
			results <- result{saved, err}
		}()
	}
	close(ready)
	group.Wait()
	close(results)

	var winner Checkpoint
	var conflicts int
	for attempt := range results {
		switch {
		case attempt.err == nil:
			if winner.Ref != "" {
				t.Fatal("both distinct answers were accepted")
			}
			winner = attempt.checkpoint
		case errors.Is(attempt.err, ErrConflict):
			conflicts++
		default:
			t.Fatalf("concurrent answer failed: %v", attempt.err)
		}
	}
	current, err := first.Current(request.Binding.JobID)
	if err != nil || conflicts != 1 || !reflect.DeepEqual(current, winner) {
		t.Fatalf("expected one persisted answer and one conflict: %+v, %d, %v", current, conflicts, err)
	}
}

func TestCheckpointRejectsWrongDraftDigestAndPathTraversal(t *testing.T) {
	dir := t.TempDir()
	store := openTestStore(t, dir)
	request := testPause("042-checkpoint")
	request.Draft = []byte("changed without a new digest")
	if _, err := store.Pause(request, ""); !errors.Is(err, ErrInvalid) {
		t.Fatalf("mismatched draft digest = %v, want ErrInvalid", err)
	}
	traversal := testPause("../outside")
	if _, err := store.Pause(traversal, ""); !errors.Is(err, ErrInvalid) {
		t.Fatalf("path traversal job = %v, want ErrInvalid", err)
	}
	if _, err := store.Resume("../outside", testPause("042-checkpoint").Binding); !errors.Is(err, ErrInvalid) {
		t.Fatalf("path traversal checkpoint = %v, want ErrInvalid", err)
	}
	entries, err := os.ReadDir(dir)
	if err != nil || len(entries) != 0 {
		t.Fatalf("invalid inputs left files: %v, %v", entries, err)
	}
}

func TestCheckpointDoesNotFollowStoreSymlinks(t *testing.T) {
	dir := t.TempDir()
	link := filepath.Join(t.TempDir(), "checkpoint-link")
	if err := os.Symlink(dir, link); err != nil {
		t.Fatal(err)
	}
	if _, err := Open(link); err == nil {
		t.Fatal("opened a symlink as the checkpoint directory")
	}
}

func TestCheckpointPauseRetryPreservesEmptyLists(t *testing.T) {
	store := openTestStore(t, t.TempDir())
	request := testPause("042-checkpoint")
	request.CompletedWork = []string{}
	request.Evidence = []string{}
	initial := pauseTestJob(t, store, request, "")
	retry := pauseTestJob(t, store, request, "")
	if !reflect.DeepEqual(initial, retry) {
		t.Fatalf("a retry changed the checkpoint: first %+v, retry %+v", initial, retry)
	}
}

func TestCheckpointCanFinishInterruptedPause(t *testing.T) {
	dir := t.TempDir()
	store := openTestStore(t, dir)
	request := testPause("042-checkpoint")
	prior := pauseTestJob(t, store, request, "")
	next := request
	next.Binding.SpecRevision = "rev-2"
	next.Question = Question{ID: "format", Prompt: "Should we export CSV or JSON?"}

	// Leave only the durable draft record, as if the writer had exited
	// before replacing the job's current pointer.
	orphan := Checkpoint{
		Ref:          checkpointRef(next, prior.Ref),
		PreviousRef:  prior.Ref,
		PauseRequest: next,
		CreatedAt:    prior.CreatedAt.Add(time.Second),
	}
	directory, err := store.lock()
	if err != nil {
		t.Fatal(err)
	}
	if err := errors.Join(writeJSON(directory, checkpointName(orphan.Ref), orphan), directory.Close()); err != nil {
		t.Fatal(err)
	}

	restarted := openTestStore(t, dir)
	history, err := restarted.History(request.Binding.JobID)
	if err != nil || !reflect.DeepEqual(history, []Checkpoint{prior}) {
		t.Fatalf("an unfinished pause became visible: %+v, %v", history, err)
	}
	if _, err := restarted.SaveAnswer(orphan.Ref, next.Binding, testAnswer()); !errors.Is(err, ErrStale) {
		t.Fatalf("answer to an unpublished question = %v, want ErrStale", err)
	}
	recovered := pauseTestJob(t, restarted, next, prior.Ref)
	if !reflect.DeepEqual(orphan, recovered) {
		t.Fatalf("pause retry did not preserve the durable work: %+v", recovered)
	}
	history, err = restarted.History(request.Binding.JobID)
	if err != nil || !reflect.DeepEqual(history, []Checkpoint{orphan, prior}) {
		t.Fatalf("checkpoint lineage after recovery: %+v, %v", history, err)
	}
}

func TestCheckpointRejectsDamagedRecordWithoutReplacingIt(t *testing.T) {
	dir := t.TempDir()
	store := openTestStore(t, dir)
	request := testPause("042-checkpoint")
	paused := pauseTestJob(t, store, request, "")
	path := filepath.Join(dir, checkpointName(paused.Ref))
	content, err := os.ReadFile(path)
	if err != nil {
		t.Fatal(err)
	}
	damaged := strings.Replace(string(content), "rev-1", "rev-2", 1)
	if damaged == string(content) {
		t.Fatal("fixture did not change the revision")
	}
	if err := os.WriteFile(path, []byte(damaged), 0600); err != nil {
		t.Fatal(err)
	}

	if _, err := store.Current(request.Binding.JobID); !errors.Is(err, ErrCorrupt) {
		t.Fatalf("load did not detect damage = %v, want ErrCorrupt", err)
	}
	if _, err := store.SaveAnswer(paused.Ref, request.Binding, testAnswer()); !errors.Is(err, ErrCorrupt) {
		t.Fatalf("answer to the damaged record = %v, want ErrCorrupt", err)
	}
	current, err := os.ReadFile(path)
	if err != nil || string(current) != damaged {
		t.Fatalf("damaged checkpoint was replaced: %v", err)
	}
}

func TestCheckpointLeavesExternalSymlinkTargetAlone(t *testing.T) {
	dir := t.TempDir()
	store := openTestStore(t, dir)
	request := testPause("042-checkpoint")
	paused := pauseTestJob(t, store, request, "")
	path := filepath.Join(dir, checkpointName(paused.Ref))
	content, err := os.ReadFile(path)
	if err != nil {
		t.Fatal(err)
	}
	target := filepath.Join(t.TempDir(), "outside-checkpoint.json")
	if err := os.Rename(path, target); err != nil {
		t.Fatal(err)
	}
	if err := os.Symlink(target, path); err != nil {
		t.Fatal(err)
	}
	if _, err := store.SaveAnswer(paused.Ref, request.Binding, testAnswer()); !errors.Is(err, ErrCorrupt) {
		t.Fatalf("answer followed a symlinked checkpoint = %v, want ErrCorrupt", err)
	}
	after, err := os.ReadFile(target)
	if err != nil || string(after) != string(content) {
		t.Fatalf("symlink target changed: %v", err)
	}
}

func TestOpenRequiresAnExplicitStoreDirectory(t *testing.T) {
	if _, err := Open(""); !errors.Is(err, ErrInvalid) {
		t.Fatalf("Open with an empty path = %v, want ErrInvalid", err)
	}
}

func TestCheckpointCanSaveAnswerForLargeDraft(t *testing.T) {
	dir := t.TempDir()
	store := openTestStore(t, dir)
	request := testPause("042-checkpoint")
	request.Draft = []byte(strings.Repeat("x", 6*1024*1024-4096))
	request.Binding.DraftDigest = DraftDigest(request.Draft)
	paused := pauseTestJob(t, store, request, "")
	answer := testAnswer()
	answer.Text = strings.Repeat("The approved export limit is 25 rows.\n", 512)

	if _, err := store.SaveAnswer(paused.Ref, request.Binding, answer); err != nil {
		t.Fatalf("save answer for a large checkpoint: %v", err)
	}
	resumed, err := openTestStore(t, dir).Resume(paused.Ref, request.Binding)
	if err != nil || resumed.Answer == nil || resumed.Answer.Answer != answer || !reflect.DeepEqual(resumed.Draft, request.Draft) {
		t.Fatalf("large checkpoint or answer failed to recover: %v", err)
	}
}

func TestCheckpointRejectsAnAnswerCopiedFromAnotherCheckpoint(t *testing.T) {
	dir := t.TempDir()
	store := openTestStore(t, dir)
	first := testPause("042-checkpoint")
	second := testPause("043-other")
	one := pauseTestJob(t, store, first, "")
	two := pauseTestJob(t, store, second, "")
	if _, err := store.SaveAnswer(one.Ref, first.Binding, testAnswer()); err != nil {
		t.Fatal(err)
	}

	answerFile, err := os.ReadFile(filepath.Join(dir, answerName(one.Ref)))
	if err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(dir, answerName(two.Ref)), answerFile, 0600); err != nil {
		t.Fatal(err)
	}
	if _, err := store.Resume(two.Ref, second.Binding); !errors.Is(err, ErrCorrupt) {
		t.Fatalf("resume with a copied answer = %v, want ErrCorrupt", err)
	}
}

func TestCheckpointRejectsNoncanonicalStoredAnswers(t *testing.T) {
	dir := t.TempDir()
	store := openTestStore(t, dir)
	request := testPause("042-checkpoint")
	paused := pauseTestJob(t, store, request, "")
	if _, err := store.SaveAnswer(paused.Ref, request.Binding, testAnswer()); err != nil {
		t.Fatal(err)
	}

	path := filepath.Join(dir, answerName(paused.Ref))
	raw, err := os.ReadFile(path)
	if err != nil {
		t.Fatal(err)
	}
	const actorField = `"actor_id":"owner-42"`
	for _, tc := range []struct {
		name string
		edit func(string) string
	}{
		{"duplicate actor", func(data string) string {
			return strings.Replace(data, actorField, actorField+`,"actor_id":"owner-99"`, 1)
		}},
		{"case-colliding actor", func(data string) string {
			return strings.Replace(data, actorField, actorField+`,"ACTOR_ID":"owner-99"`, 1)
		}},
		{"alternate escaping", func(data string) string {
			return strings.Replace(data, actorField, `"actor_id":"own\u0065r-42"`, 1)
		}},
		{"trailing whitespace", func(data string) string {
			return data + "\n"
		}},
	} {
		t.Run(tc.name, func(t *testing.T) {
			modified := tc.edit(string(raw))
			if modified == string(raw) {
				t.Fatal("failed to modify stored answer")
			}
			if err := os.WriteFile(path, []byte(modified), 0600); err != nil {
				t.Fatal(err)
			}
			recovered, err := store.Resume(paused.Ref, request.Binding)
			if !errors.Is(err, ErrCorrupt) || recovered.Answer != nil {
				t.Fatalf("resume used a noncanonical answer: %+v, %v", recovered.Answer, err)
			}
		})
	}
}

func TestOpenRejectsWritableStoreForOtherUsers(t *testing.T) {
	for _, mode := range []os.FileMode{0720, 0702} {
		t.Run(mode.String(), func(t *testing.T) {
			dir := t.TempDir()
			if err := os.Chmod(dir, mode); err != nil {
				t.Fatal(err)
			}
			if _, err := Open(dir); !errors.Is(err, ErrInvalid) {
				t.Fatalf("Open with mode %o = %v, want ErrInvalid", mode, err)
			}
		})
	}
}

func TestOpenRejectsDirectoryOwnedByAnotherUser(t *testing.T) {
	dir := t.TempDir()
	// A user namespace may expose only one UID, so compare against a
	// different expected UID instead of depending on ownership of "/".
	differentUID := uint32(os.Geteuid()) + 1
	if _, err := open(dir, differentUID); !errors.Is(err, ErrInvalid) {
		t.Fatalf("Open with a different owner = %v, want ErrInvalid", err)
	}
}

func TestCheckpointStaysInOriginalDirectoryWhenParentAliasChanges(t *testing.T) {
	parent := t.TempDir()
	first := filepath.Join(parent, "first", "checkpoints")
	second := filepath.Join(parent, "second", "checkpoints")
	for _, dir := range []string{first, second} {
		if err := os.MkdirAll(dir, 0700); err != nil {
			t.Fatal(err)
		}
	}
	alias := filepath.Join(parent, "alias")
	if err := os.Symlink(filepath.Dir(first), alias); err != nil {
		t.Fatal(err)
	}
	store := openTestStore(t, filepath.Join(alias, "checkpoints"))
	if err := os.Remove(alias); err != nil {
		t.Fatal(err)
	}
	if err := os.Symlink(filepath.Dir(second), alias); err != nil {
		t.Fatal(err)
	}

	request := testPause("042-checkpoint")
	saved := pauseTestJob(t, store, request, "")
	original, err := openTestStore(t, first).Current(request.Binding.JobID)
	if err != nil || original.Ref != saved.Ref {
		t.Fatalf("original store checkpoint = %+v, %v", original, err)
	}
	if _, err := openTestStore(t, second).Current(request.Binding.JobID); !errors.Is(err, ErrNotFound) {
		t.Fatalf("replacement alias contains a checkpoint: %v", err)
	}
}

func TestCheckpointRefusesAReplacedDirectory(t *testing.T) {
	dir := filepath.Join(t.TempDir(), "checkpoints")
	if err := os.Mkdir(dir, 0700); err != nil {
		t.Fatal(err)
	}
	store := openTestStore(t, dir)
	if err := os.Rename(dir, dir+"-prior"); err != nil {
		t.Fatal(err)
	}
	if err := os.Mkdir(dir, 0700); err != nil {
		t.Fatal(err)
	}

	if _, err := store.Pause(testPause("042-checkpoint"), ""); !errors.Is(err, ErrInvalid) {
		t.Fatalf("pause with a replaced directory = %v, want ErrInvalid", err)
	}
	if entries, err := os.ReadDir(dir); err != nil || len(entries) != 0 {
		t.Fatalf("checkpoint was written to the new directory: %v, %v", entries, err)
	}
}
