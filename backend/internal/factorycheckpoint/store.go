package factorycheckpoint

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

const maxRecordBytes = 8 * 1024 * 1024

// Store keeps checkpoints in a private local directory shared by its callers.
type Store struct {
	dir      string
	identity os.FileInfo
}

// Open opens a directory owned by the effective user, with no group or world
// write access. Keep the resolved directory and its parents under trusted
// ownership and at stable paths during operations.
func Open(dir string) (*Store, error) {
	return open(dir, uint32(os.Geteuid()))
}

func open(dir string, expectedUID uint32) (*Store, error) {
	if dir == "" {
		return nil, ErrInvalid
	}
	dir, err := filepath.Abs(dir)
	if err != nil {
		return nil, err
	}
	file, err := openDirectory(dir)
	if err != nil {
		return nil, err
	}
	defer file.Close()

	identity, err := privateDirectory(file, expectedUID)
	if err != nil {
		return nil, err
	}
	dir, err = filepath.EvalSymlinks(dir)
	if err != nil {
		return nil, err
	}
	if err := verifyDirectoryPath(dir, identity); err != nil {
		return nil, err
	}
	return &Store{dir: dir, identity: identity}, nil
}

func openDirectory(dir string) (*os.File, error) {
	fd, err := syscall.Open(dir, syscall.O_RDONLY|syscall.O_NOFOLLOW|syscall.O_DIRECTORY|syscall.O_CLOEXEC, 0)
	if err != nil {
		return nil, fmt.Errorf("open checkpoint directory: %w", err)
	}
	return os.NewFile(uintptr(fd), dir), nil
}

func privateDirectory(file *os.File, expectedUID uint32) (os.FileInfo, error) {
	info, err := file.Stat()
	if err != nil {
		return nil, err
	}
	owner := info.Sys().(*syscall.Stat_t).Uid
	if owner != expectedUID || info.Mode().Perm()&0022 != 0 {
		return nil, fmt.Errorf("%w: checkpoint directory must be owned by the effective user and writable only by its owner", ErrInvalid)
	}
	return info, nil
}

func verifyDirectoryPath(path string, expected os.FileInfo) error {
	current, err := os.Lstat(path)
	if err != nil {
		return err
	}
	if !os.SameFile(current, expected) {
		return fmt.Errorf("%w: checkpoint directory was replaced", ErrInvalid)
	}
	return nil
}

func (s *Store) lock() (*os.File, error) {
	file, err := openDirectory(s.dir)
	if err != nil {
		return nil, err
	}

	// The lock is on the directory inode and is released when file closes,
	// including when the process exits during a checkpoint operation.
	for {
		err = syscall.Flock(int(file.Fd()), syscall.LOCK_EX)
		if !errors.Is(err, syscall.EINTR) {
			break
		}
	}
	if err != nil {
		file.Close()
		return nil, fmt.Errorf("lock checkpoint directory: %w", err)
	}
	held, err := privateDirectory(file, uint32(os.Geteuid()))
	if err != nil {
		file.Close()
		return nil, err
	}
	if !os.SameFile(held, s.identity) {
		file.Close()
		return nil, fmt.Errorf("%w: checkpoint directory was replaced", ErrInvalid)
	}
	if err := verifyDirectoryPath(s.dir, held); err != nil {
		file.Close()
		return nil, err
	}
	return file, nil
}

func readJSON[T any](dir, name string) (T, error) {
	var value T
	path := filepath.Join(dir, name)
	fd, err := syscall.Open(path, syscall.O_RDONLY|syscall.O_NOFOLLOW|syscall.O_NONBLOCK|syscall.O_CLOEXEC, 0)
	if err != nil {
		return value, fmt.Errorf("open checkpoint record: %w", err)
	}
	file := os.NewFile(uintptr(fd), path)
	defer file.Close()

	info, err := file.Stat()
	if err != nil {
		return value, err
	}
	if !info.Mode().IsRegular() || info.Size() > maxRecordBytes {
		return value, ErrCorrupt
	}
	data, err := io.ReadAll(io.LimitReader(file, maxRecordBytes+1))
	if err != nil {
		return value, err
	}
	if len(data) > maxRecordBytes {
		return value, ErrCorrupt
	}

	if err := json.Unmarshal(data, &value); err != nil {
		return value, fmt.Errorf("%w: decode checkpoint: %w", ErrCorrupt, err)
	}
	// Compare with the exact writer format. Unmarshal alone accepts duplicate
	// keys and matches field names without regard to case.
	canonical, err := json.Marshal(value)
	if err != nil || !bytes.Equal(data, canonical) {
		return value, ErrCorrupt
	}
	return value, nil
}

func writeJSON(directory *os.File, name string, value any) error {
	data, err := json.Marshal(value)
	if err != nil {
		return err
	}
	if len(data) > maxRecordBytes {
		return fmt.Errorf("%w: checkpoint record exceeds %d bytes", ErrInvalid, maxRecordBytes)
	}

	file, err := os.CreateTemp(directory.Name(), ".checkpoint-*")
	if err != nil {
		return err
	}
	defer os.Remove(file.Name())

	_, err = file.Write(data)
	if err == nil {
		err = file.Sync()
	}
	if err := errors.Join(err, file.Close()); err != nil {
		return err
	}
	if err := os.Rename(file.Name(), filepath.Join(directory.Name(), name)); err != nil {
		return err
	}
	return directory.Sync()
}
