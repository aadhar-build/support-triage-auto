package factoryproposer

import (
	"crypto/sha256"
	"errors"
	"fmt"
	"io"
	"io/fs"
	"os"
	"path/filepath"
	"regexp"
	"syscall"
)

var codexDatabase = regexp.MustCompile(`^(state|logs)_[0-9]+\.sqlite(-[a-z]+)?$`)

// Each digest was reproduced twice with the installed native Codex
// prompt-input debugger in clean, unauthenticated profiles. Verify a clean
// bundle before accepting changes to its bundled files.
const (
	codex0164SystemSkills = "8b866e5df68dc414bee9aabe1f124e449a919297b5e12c45ed3191e675d15351"
	codex0170SystemSkills = "9491f4e522e3acd6f0461c83cae40191728003df648c3de27512b0df3594e69c"
)

func (c cli) configDir() string {
	return filepath.Join(c.home, "."+c.name)
}

// A native CLI may create runtime state alongside its credentials. Allow the
// known state paths and refuse user settings, instructions, and custom skills.
// This checks the private profile at launch; host-managed policy is trusted.
func (c cli) safeProfile() bool {
	if privateHome(c.home) != c.home {
		return false
	}
	home, err := os.ReadDir(c.home)
	if err != nil {
		return false
	}
	for _, entry := range home {
		switch {
		case entry.Name() == "."+c.name && safeDirectory(entry):
		case c.name == "claude" && entry.Name() == ".claude.json" && safeFile(entry, false):
		default:
			return false
		}
	}
	config, err := os.ReadDir(c.configDir())
	if errors.Is(err, os.ErrNotExist) {
		return true
	}
	if err != nil {
		return false
	}
	for _, entry := range config {
		if c.name == "claude" && !safeClaudeEntry(entry) || c.name == "codex" && !safeCodexEntry(c.configDir(), entry) {
			return false
		}
	}
	return true
}

func safeClaudeEntry(entry os.DirEntry) bool {
	switch entry.Name() {
	case ".credentials.json", ".credentials.json.lock":
		return safeFile(entry, true)
	case ".claude.json", ".claude.json.lock":
		return safeFile(entry, false)
	case "backups", "cache", "debug", "statsig", "telemetry":
		return safeDirectory(entry)
	}
	return false
}

func safeCodexEntry(dir string, entry os.DirEntry) bool {
	switch entry.Name() {
	case "auth.json":
		return safeFile(entry, true)
	case "installation_id", ".sandbox_migration", "models_cache.json", "version.json":
		return safeFile(entry, false)
	case "tmp", "log", "logs":
		return safeDirectory(entry)
	case "skills":
		if !safeDirectory(entry) {
			return false
		}
		children, err := os.ReadDir(filepath.Join(dir, "skills"))
		if err != nil {
			return false
		}
		if len(children) == 0 {
			return true
		}
		return len(children) == 1 && children[0].Name() == ".system" &&
			safeDirectory(children[0]) && trustedCodexBundle(filepath.Join(dir, "skills", ".system"))
	}
	return codexDatabase.MatchString(entry.Name()) && safeFile(entry, false)
}

func trustedCodexBundle(dir string) bool {
	digest := sha256.New()
	const maxBytes = 8 << 20
	var usedBytes int64
	err := filepath.WalkDir(dir, func(path string, entry fs.DirEntry, walkErr error) error {
		if walkErr != nil {
			return walkErr
		}
		relative, err := filepath.Rel(dir, path)
		if err != nil {
			return err
		}
		info, err := entry.Info()
		if err != nil || !controlledByUser(info) {
			return errors.New("untrusted Codex bundle entry")
		}
		if info.IsDir() {
			fmt.Fprintf(digest, "dir\x00%s\x000\x00", filepath.ToSlash(relative))
			return nil
		}
		if !regularSingleLink(info) || info.Size() < 0 || info.Size() > maxBytes-usedBytes {
			return errors.New("untrusted Codex bundle file")
		}
		usedBytes += info.Size()
		file, err := os.Open(path)
		if err != nil {
			return err
		}
		defer file.Close()
		opened, err := file.Stat()
		if err != nil || !os.SameFile(info, opened) || !controlledByUser(opened) || !regularSingleLink(opened) {
			return errors.New("Codex bundle file changed while opening")
		}
		fmt.Fprintf(digest, "file\x00%s\x00%d\x00", filepath.ToSlash(relative), info.Size())
		n, err := io.Copy(digest, io.LimitReader(file, info.Size()+1))
		if err != nil || n != info.Size() {
			return errors.New("Codex bundle file changed while reading")
		}
		return nil
	})
	if err != nil {
		return false
	}
	hash := fmt.Sprintf("%x", digest.Sum(nil))
	return hash == codex0164SystemSkills || hash == codex0170SystemSkills
}

func safeDirectory(entry os.DirEntry) bool {
	info, err := entry.Info()
	return err == nil && info.IsDir() && controlledByUser(info)
}

func safeFile(entry os.DirEntry, private bool) bool {
	info, err := entry.Info()
	return err == nil && controlledByUser(info) && regularSingleLink(info) && (!private || info.Mode().Perm()&0077 == 0)
}

func controlledByUser(info os.FileInfo) bool {
	stat, ok := info.Sys().(*syscall.Stat_t)
	return ok && int(stat.Uid) == os.Geteuid() && info.Mode().Perm()&0022 == 0
}

func regularSingleLink(info os.FileInfo) bool {
	stat, ok := info.Sys().(*syscall.Stat_t)
	return ok && info.Mode().IsRegular() && stat.Nlink == 1
}
