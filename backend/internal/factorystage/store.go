package factorystage

import (
	"bytes"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"os"
	"path/filepath"
	"syscall"
)

const (
	logName     = "stage-events-v1.jsonl"
	maxLogBytes = 8 * 1024 * 1024
)

// ErrLogTooLarge indicates that a log exceeds, or an append would exceed, 8 MiB.
var ErrLogTooLarge = fmt.Errorf("stage event log exceeds %d bytes", maxLogBytes)

// Store holds a stage log in a controller-owned directory on a local filesystem.
// Callers must keep the directory and its trusted parent path stable. All
// readers and writers must use the store so its directory lock is respected.
type Store struct {
	dir      string
	identity os.FileInfo
}

// Open locates the store in an existing directory owned by the effective user.
// The final path component must be a directory with group and other write
// permissions disabled. The first successful append creates the log.
func Open(dir string) (*Store, error) {
	if dir == "" {
		return nil, errors.New("missing stage event store directory")
	}
	absolute, err := filepath.Abs(dir)
	if err != nil {
		return nil, fmt.Errorf("resolve stage event store directory: %w", err)
	}
	file, err := openDirectory(absolute)
	if err != nil {
		return nil, err
	}
	info, err := file.Stat()
	if err := errors.Join(err, file.Close()); err != nil {
		return nil, fmt.Errorf("inspect stage event store directory: %w", err)
	}
	if err := validateDirectory(info); err != nil {
		return nil, err
	}
	return &Store{dir: absolute, identity: info}, nil
}

// Append durably publishes a new event or returns false for an identical event
// already present. Differences under the same EventID return ErrConflict.
// An append that would exceed the log size limit returns ErrLogTooLarge.
// An I/O failure may leave a published event. Retry with the original event.
func (s *Store) Append(event Event) (bool, error) {
	event.OccurredAt = event.OccurredAt.UTC()
	if err := event.validate(); err != nil {
		return false, err
	}
	encoded, err := json.Marshal(event)
	if err != nil {
		return false, fmt.Errorf("%w: encode event: %v", ErrInvalidEvent, err)
	}

	dir, err := s.lockDirectory(syscall.LOCK_EX)
	if err != nil {
		return false, err
	}
	defer dir.Close()

	raw, err := s.readLog()
	if err != nil {
		return false, err
	}
	events, err := replay(raw)
	if err != nil {
		return false, err
	}
	if err := s.checkDirectory(); err != nil {
		return false, err
	}
	for _, prior := range events {
		if event.EventID == prior.EventID {
			if event != prior {
				return false, ErrConflict
			}
			// The last attempt may have stopped after rename and before sync.
			return false, syncDirectory(dir)
		}
	}
	if len(encoded)+1 > maxLogBytes-len(raw) {
		return false, ErrLogTooLarge
	}
	if err := s.publish(dir, raw, append(encoded, '\n')); err != nil {
		return false, err
	}
	return true, nil
}

// ReadAll returns a snapshot in append order. Invalid logs return ErrCorrupt,
// oversized logs return ErrLogTooLarge, and errors return no partial history.
func (s *Store) ReadAll() ([]Event, error) {
	dir, err := s.lockDirectory(syscall.LOCK_SH)
	if err != nil {
		return nil, err
	}
	defer dir.Close()

	raw, err := s.readLog()
	if err != nil {
		return nil, err
	}
	events, err := replay(raw)
	if err != nil {
		return nil, err
	}
	if err := s.checkDirectory(); err != nil {
		return nil, err
	}
	return events, nil
}

func openDirectory(path string) (*os.File, error) {
	dir, err := os.OpenFile(path, os.O_RDONLY|syscall.O_DIRECTORY|syscall.O_NOFOLLOW, 0)
	if err != nil {
		return nil, fmt.Errorf("open stage event store directory: %w", err)
	}
	return dir, nil
}

func validateDirectory(info os.FileInfo) error {
	stat, ok := info.Sys().(*syscall.Stat_t)
	if !info.IsDir() || !ok || stat.Uid != uint32(os.Geteuid()) || info.Mode().Perm()&0022 != 0 {
		return errors.New("stage event store directory must be owned by the current user with group and other write permissions disabled")
	}
	return nil
}

func (s *Store) checkDirectory() error {
	current, err := os.Lstat(s.dir)
	if err != nil {
		return fmt.Errorf("inspect stage event store directory: %w", err)
	}
	if !os.SameFile(s.identity, current) {
		return errors.New("stage event store directory changed since open")
	}
	return validateDirectory(current)
}

func (s *Store) lockDirectory(mode int) (*os.File, error) {
	// Lock the directory inode because every append replaces the log inode.
	dir, err := openDirectory(s.dir)
	if err != nil {
		return nil, err
	}
	info, err := dir.Stat()
	if err != nil {
		dir.Close()
		return nil, fmt.Errorf("inspect stage event store directory: %w", err)
	}
	if !os.SameFile(s.identity, info) {
		dir.Close()
		return nil, errors.New("stage event store directory changed since open")
	}
	if err := validateDirectory(info); err != nil {
		dir.Close()
		return nil, err
	}
	for {
		err = syscall.Flock(int(dir.Fd()), mode)
		if !errors.Is(err, syscall.EINTR) {
			break
		}
	}
	if err != nil {
		dir.Close()
		return nil, fmt.Errorf("lock stage event store directory: %w", err)
	}
	if err := s.checkDirectory(); err != nil {
		dir.Close()
		return nil, err
	}
	return dir, nil
}

func (s *Store) readLog() ([]byte, error) {
	path := filepath.Join(s.dir, logName)
	// Refuse symlinks and ensure a special file cannot block the reader.
	file, err := os.OpenFile(path, os.O_RDONLY|syscall.O_NOFOLLOW|syscall.O_NONBLOCK, 0)
	if errors.Is(err, os.ErrNotExist) {
		return nil, nil
	}
	if errors.Is(err, syscall.ELOOP) {
		return nil, fmt.Errorf("%w: log is a symlink", ErrCorrupt)
	}
	if err != nil {
		return nil, fmt.Errorf("open stage event log: %w", err)
	}
	defer file.Close()

	info, err := file.Stat()
	if err != nil {
		return nil, fmt.Errorf("inspect stage event log: %w", err)
	}
	if !info.Mode().IsRegular() {
		return nil, fmt.Errorf("%w: log is not a regular file", ErrCorrupt)
	}
	if info.Size() > maxLogBytes {
		return nil, ErrLogTooLarge
	}
	raw, err := io.ReadAll(io.LimitReader(file, maxLogBytes+1))
	if err != nil {
		return nil, fmt.Errorf("read stage event log: %w", err)
	}
	if len(raw) > maxLogBytes {
		return nil, ErrLogTooLarge
	}
	if len(raw) == 0 {
		return nil, fmt.Errorf("%w: empty log", ErrCorrupt)
	}
	return raw, nil
}

func replay(raw []byte) ([]Event, error) {
	var events []Event
	seen := make(map[string]struct{})
	for len(raw) > 0 {
		number := len(events) + 1
		record, remaining, complete := bytes.Cut(raw, []byte{'\n'})
		if !complete {
			return nil, fmt.Errorf("%w: truncated record at line %d", ErrCorrupt, number)
		}
		raw = remaining

		var event Event
		if err := json.Unmarshal(record, &event); err != nil {
			return nil, fmt.Errorf("%w: invalid JSON at line %d", ErrCorrupt, number)
		}
		event.OccurredAt = event.OccurredAt.UTC()
		if err := event.validate(); err != nil {
			return nil, fmt.Errorf("%w: invalid record at line %d: %v", ErrCorrupt, number, err)
		}
		// Only our canonical encoding is accepted, including exact field names
		// and UTC timestamps. This also rejects duplicate or unknown fields.
		canonical, err := json.Marshal(event)
		if err != nil || !bytes.Equal(record, canonical) {
			return nil, fmt.Errorf("%w: noncanonical record at line %d", ErrCorrupt, number)
		}
		if _, duplicate := seen[event.EventID]; duplicate {
			return nil, fmt.Errorf("%w: duplicate event ID at line %d", ErrCorrupt, number)
		}
		seen[event.EventID] = struct{}{}
		events = append(events, event)
	}
	return events, nil
}

func (s *Store) publish(dir *os.File, history, record []byte) error {
	file, err := os.CreateTemp(s.dir, ".stage-events-")
	if err != nil {
		return fmt.Errorf("stage event log snapshot: %w", err)
	}
	defer os.Remove(file.Name())

	_, err = file.Write(history)
	if err == nil {
		_, err = file.Write(record)
	}
	if err == nil {
		err = file.Sync()
	}
	if err := errors.Join(err, file.Close()); err != nil {
		return fmt.Errorf("write stage event log snapshot: %w", err)
	}
	if err := s.checkDirectory(); err != nil {
		return err
	}
	if err := os.Rename(file.Name(), filepath.Join(s.dir, logName)); err != nil {
		return fmt.Errorf("publish stage event log snapshot: %w", err)
	}
	return syncDirectory(dir)
}

func syncDirectory(dir *os.File) error {
	if err := dir.Sync(); err != nil {
		return fmt.Errorf("sync stage event store directory: %w", err)
	}
	return nil
}
