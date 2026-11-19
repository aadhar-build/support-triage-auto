package main

import (
	"bytes"
	"context"
	"errors"
	"fmt"
	"io/fs"
	"os"
	"path/filepath"
	"strings"
	"syscall"
	"time"
)

// Python job commands flock the same directory inode. Keep the descriptor
// open between admission checks so a replaced job directory cannot be used.
func withJobAdmission(ctx context.Context, dir *os.File, state []byte, recoveryPending string, action func() error) (err error) {
	if err := lockJobDirectory(ctx, dir); err != nil {
		return err
	}
	defer func() {
		if unlockErr := syscall.Flock(int(dir.Fd()), syscall.LOCK_UN); unlockErr != nil {
			err = errors.Join(err, fmt.Errorf("release job admission: %w", unlockErr))
		}
	}()

	jobDir := dir.Name()
	opened, err := dir.Stat()
	if err != nil {
		return err
	}
	current, err := os.Lstat(jobDir)
	if err != nil || !current.IsDir() || !os.SameFile(opened, current) {
		return errors.New("job directory changed during dispatch; reload the job and retry")
	}
	for _, marker := range []string{".factory-operation.json", ".gate-publication.pending"} {
		if _, err := os.Lstat(filepath.Join(jobDir, marker)); !errors.Is(err, os.ErrNotExist) {
			return fmt.Errorf("job operation is active or interrupted (%s); wait for it to finish or inspect it before retrying", marker)
		}
	}
	if err := blockUnrecoveredBuilders(ctx, jobDir, recoveryPending); err != nil {
		return err
	}
	currentState, err := readLiveEvidence(jobDir, "job.json", maxJobInputBytes)
	if err != nil || !bytes.Equal(state, currentState) {
		return errors.New("job state changed during dispatch; reload the job and retry")
	}
	return action()
}

func lockJobDirectory(ctx context.Context, dir *os.File) error {
	retry, cancel := context.WithTimeout(ctx, 250*time.Millisecond)
	defer cancel()
	ticker := time.NewTicker(10 * time.Millisecond)
	defer ticker.Stop()
	for {
		if err := ctx.Err(); err != nil {
			return err
		}
		err := syscall.Flock(int(dir.Fd()), syscall.LOCK_EX|syscall.LOCK_NB)
		if err == nil {
			return nil
		}
		if err != syscall.EWOULDBLOCK && err != syscall.EAGAIN && err != syscall.EINTR {
			return fmt.Errorf("acquire job admission: %w", err)
		}
		select {
		case <-retry.Done():
			return fmt.Errorf("job %s is busy with another factory operation; retry shortly", filepath.Base(dir.Name()))
		case <-ticker.C:
		}
	}
}

func blockUnrecoveredBuilders(ctx context.Context, jobDir, recoveryPending string) error {
	root, err := os.OpenRoot(jobDir)
	if err != nil {
		return err
	}
	defer root.Close()
	entries := 0
	return scanJobDirectory(ctx, root, func(entry fs.DirEntry) error {
		entries++
		if entries > maxJobDirectoryEntries {
			return errors.New("job directory has too many entries to check for pending builder publications")
		}
		name := entry.Name()
		if strings.HasPrefix(name, "worker-round-") && strings.HasSuffix(name, "-builder.json.pending") &&
			filepath.Join(jobDir, name) != recoveryPending {
			return fmt.Errorf("builder publication is pending (%s); run worker --recover %s or inspect the interrupted round", name, filepath.Base(jobDir))
		}
		return nil
	})
}
