package factorystate

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"io/fs"
	"os"
	"os/exec"
	"path/filepath"
	"slices"
	"strings"
	"syscall"
	"time"
	"unicode/utf8"
)

type object map[string]json.RawMessage

// decodeObject rejects duplicate keys at every level used by a decision.
// Unknown fields may contain native metadata which this reader never consumes.
func decodeObject(raw []byte) (object, error) {
	invalid := errors.New("invalid JSON record")
	if !utf8.Valid(raw) {
		return nil, invalid
	}
	decoder := json.NewDecoder(bytes.NewReader(raw))
	first, err := decoder.Token()
	if err != nil || first != json.Delim('{') {
		return nil, invalid
	}
	fields := object{}
	for decoder.More() {
		token, err := decoder.Token()
		name, isName := token.(string)
		if err != nil || !isName || fields[name] != nil {
			return nil, invalid
		}
		var value json.RawMessage
		if err := decoder.Decode(&value); err != nil {
			return nil, invalid
		}
		fields[name] = value
	}
	last, err := decoder.Token()
	if err != nil || last != json.Delim('}') {
		return nil, invalid
	}
	if _, err := decoder.Token(); !errors.Is(err, io.EOF) {
		return nil, invalid
	}
	return fields, nil
}

func (o object) field(key string, target any) bool {
	raw, ok := o[key]
	return ok && !bytes.Equal(raw, []byte("null")) && json.Unmarshal(raw, target) == nil
}

type directory struct {
	parent *os.Root
	name   string
	root   *os.Root
	lock   *os.File
	info   fs.FileInfo
}

func openCheckout(path string) (*os.Root, error) {
	refused := errors.New("factory checkout must be a real directory")
	info, err := os.Lstat(path)
	if err != nil || !info.IsDir() {
		return nil, refused
	}
	root, err := os.OpenRoot(path)
	if err != nil {
		return nil, refused
	}
	opened, err := root.Stat(".")
	if err != nil || !os.SameFile(info, opened) {
		root.Close()
		return nil, refused
	}
	return root, nil
}

func openDirectory(parent *os.Root, name string, lock bool) (*directory, error) {
	invalid := fmt.Errorf("invalid or unavailable factory directory %s", name)
	info, err := parent.Lstat(name)
	if err != nil || !info.IsDir() {
		return nil, invalid
	}
	sub, err := parent.OpenRoot(name)
	if err != nil {
		return nil, invalid
	}
	dir := &directory{parent: parent, name: name, root: sub, info: info}
	if opened, err := sub.Stat("."); err != nil || !os.SameFile(info, opened) {
		dir.close()
		return nil, invalid
	}
	if lock {
		dir.lock, err = sub.OpenFile(".", os.O_RDONLY|syscall.O_DIRECTORY, 0)
		if err != nil {
			dir.close()
			return nil, invalid
		}
		err = syscall.Flock(int(dir.lock.Fd()), syscall.LOCK_SH|syscall.LOCK_NB)
		if err != nil {
			dir.close()
			if errors.Is(err, syscall.EWOULDBLOCK) || errors.Is(err, syscall.EAGAIN) {
				return nil, fmt.Errorf("%w for %s", ErrBusy, name)
			}
			return nil, invalid
		}
	}
	if !dir.matches() {
		dir.close()
		return nil, invalid
	}
	return dir, nil
}

func (d *directory) matches() bool {
	current, err := d.parent.Lstat(d.name)
	return err == nil && os.SameFile(d.info, current)
}

func (d *directory) close() {
	if d.lock != nil {
		d.lock.Close()
	}
	d.root.Close()
}

func directoryEntries(root *os.Root, limit int) ([]fs.DirEntry, error) {
	dir, err := root.OpenFile(".", os.O_RDONLY|syscall.O_DIRECTORY, 0)
	if err != nil {
		return nil, err
	}
	defer dir.Close()
	entries, err := dir.ReadDir(limit + 1)
	if len(entries) > limit || (err != nil && !errors.Is(err, io.EOF)) {
		return nil, errors.New("incomplete factory directory read")
	}
	return entries, nil
}

func recheckInventory(root *os.Root, names []string, locked []*directory) error {
	current, err := listJobs(root)
	if err != nil || !slices.Equal(names, current) {
		return errors.New("factory jobs changed during observation")
	}
	for _, dir := range locked {
		if !dir.matches() {
			return errors.New("factory job directory changed during observation")
		}
	}
	return nil
}

func readRegular(root *os.Root, name string, limit int64) ([]byte, error) {
	info, err := root.Lstat(name)
	if err != nil {
		return nil, err
	}
	invalid := errors.New("invalid, oversized or changing factory record")
	if !info.Mode().IsRegular() || info.Size() > limit {
		return nil, invalid
	}
	file, err := root.OpenFile(name, os.O_RDONLY|syscall.O_NOFOLLOW|syscall.O_NONBLOCK, 0)
	if err != nil {
		return nil, err
	}
	defer file.Close()
	opened, err := file.Stat()
	if err != nil || !opened.Mode().IsRegular() || !sameRecord(info, opened) {
		return nil, invalid
	}
	raw, err := io.ReadAll(io.LimitReader(file, limit+1))
	if err != nil || int64(len(raw)) != info.Size() || int64(len(raw)) > limit {
		return nil, invalid
	}
	current, err := root.Lstat(name)
	if err != nil || !sameRecord(opened, current) {
		return nil, invalid
	}
	return raw, nil
}

func sameRecord(a, b fs.FileInfo) bool {
	return os.SameFile(a, b) && a.Size() == b.Size() && a.ModTime() == b.ModTime()
}

func resolveCommit(ctx context.Context, checkout, ref string) (string, error) {
	bounded, cancel := context.WithTimeout(ctx, 2*time.Second)
	defer cancel()
	cmd := exec.CommandContext(bounded, "git", "--git-dir="+filepath.Join(checkout, ".git"),
		"rev-parse", "--verify", "--end-of-options", ref+"^{commit}")
	cmd.Dir = checkout
	cmd.Env = []string{
		"GIT_CONFIG_NOSYSTEM=1", "GIT_CONFIG_GLOBAL=" + os.DevNull,
		"GIT_NO_LAZY_FETCH=1", "GIT_NO_REPLACE_OBJECTS=1", "GIT_OPTIONAL_LOCKS=0", "LC_ALL=C",
		"GIT_ALLOW_PROTOCOL=",
	}
	out, err := cmd.Output()
	if err := bounded.Err(); err != nil {
		return "", err
	}
	sha := strings.TrimSuffix(string(out), "\n")
	if err != nil || !commitID.MatchString(sha) {
		return "", errors.New("cannot verify the local factory Git commit")
	}
	return sha, nil
}

func readBase(ctx context.Context, checkout, ref string) (string, error) {
	base, err := resolveCommit(ctx, checkout, ref)
	if err != nil {
		return "", err
	}
	head, err := resolveCommit(ctx, checkout, "HEAD")
	if err != nil {
		return "", err
	}
	if head != base {
		return "", ErrStaleCheckout
	}
	return base, nil
}
