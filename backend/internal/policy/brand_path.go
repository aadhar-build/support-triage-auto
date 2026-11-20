package policy

import (
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"regexp"
	"strings"
	"syscall"
	"unicode/utf8"
)

// BrandError is a brand-path or rules-source failure whose message Python
// raises with no "<source>: " prefix. Callers treat it like a *RulesError.
type BrandError struct{ Msg string }

func (e *BrandError) Error() string { return e.Msg }

var brandName = regexp.MustCompile(`^[a-z0-9][a-z0-9_-]{0,63}$`)

// BrandPath is brand_path: the resolved guidelines.md of brand name, refused
// unless it lies strictly inside brandsDir with symlinks followed. The name is
// checked before any filesystem access.
func BrandPath(name, brandsDir string) (string, error) {
	if !brandName.MatchString(name) {
		return "", &BrandError{fmt.Sprintf("bad brand name %s; expected [a-z0-9][a-z0-9_-]{0,63}", reprArg(name))}
	}
	root := pyResolve(brandsDir)
	if isLoop(root) {
		return "", loopError(name, root)
	}
	resolved := pyResolve(root + "/" + name + "/guidelines.md")
	if isLoop(resolved) {
		return "", loopError(name, root)
	}
	if !hasStrictAncestor(resolved, root) {
		return "", &BrandError{fmt.Sprintf("brand '%s' resolves outside %s", name, root)}
	}
	if st, err := os.Stat(resolved); err != nil || !st.Mode().IsRegular() {
		return "", &BrandError{fmt.Sprintf("brand '%s' not found under %s", name, root)}
	}
	return resolved, nil
}

func loopError(name, root string) error {
	return &BrandError{fmt.Sprintf("brand '%s' resolves through a symlink loop under %s", name, root)}
}

// isLoop is the ELOOP check Python's Path.resolve makes after realpath.
func isLoop(p string) bool {
	_, err := os.Stat(p)
	return errors.Is(err, syscall.ELOOP)
}

// hasStrictAncestor is Python's `root in path.parents` for normalised
// absolute paths: a component-wise strict prefix.
func hasStrictAncestor(path, root string) bool {
	if root == "/" {
		return path != "/"
	}
	return strings.HasPrefix(path, root+"/")
}

// reprArg is Python's repr of an argv string: bytes that are not valid UTF-8
// were decoded with surrogateescape and print as \udcXX.
func reprArg(s string) string {
	if utf8.ValidString(s) {
		return reprStr(s)
	}
	// Swap each bad byte for a private placeholder rune, repr, then patch.
	var b strings.Builder
	var bad []byte
	for i := 0; i < len(s); {
		r, size := utf8.DecodeRuneInString(s[i:])
		if r == utf8.RuneError && size == 1 {
			b.WriteRune(0x10fff0)
			bad = append(bad, s[i])
		} else {
			b.WriteString(s[i : i+size])
		}
		i += size
	}
	out := reprStr(b.String())
	for _, c := range bad {
		out = strings.Replace(out, `\U0010fff0`, fmt.Sprintf(`\udc%02x`, c), 1)
	}
	return out
}

// pyResolve is CPython 3.12's non-strict os.path.realpath, which
// Path.resolve() runs: a port of posixpath._joinrealpath, then abspath.
func pyResolve(p string) string {
	path, _ := joinRealpath("", p, map[string]*string{})
	if !strings.HasPrefix(path, "/") {
		wd, err := os.Getwd()
		if err == nil {
			path = wd + "/" + path
		}
	}
	return filepath.Clean(path)
}

func pyJoin(a, b string) string {
	if strings.HasPrefix(b, "/") {
		return b
	}
	if a == "" || strings.HasSuffix(a, "/") {
		return a + b
	}
	return a + "/" + b
}

func pySplit(p string) (string, string) {
	i := strings.LastIndex(p, "/") + 1
	head, tail := p[:i], p[i:]
	if head != "" && strings.Trim(head, "/") != "" {
		head = strings.TrimRight(head, "/")
	}
	return head, tail
}

func joinRealpath(path, rest string, seen map[string]*string) (string, bool) {
	if strings.HasPrefix(rest, "/") {
		rest = rest[1:]
		path = "/"
	}
	for rest != "" {
		var name string
		name, rest, _ = strings.Cut(rest, "/")
		if name == "" || name == "." {
			continue
		}
		if name == ".." {
			if path != "" {
				var tail string
				path, tail = pySplit(path)
				if tail == ".." {
					path = pyJoin(pyJoin(path, ".."), "..")
				}
			} else {
				path = ".."
			}
			continue
		}
		newpath := pyJoin(path, name)
		st, err := os.Lstat(newpath)
		if err != nil || st.Mode()&os.ModeSymlink == 0 {
			path = newpath
			continue
		}
		if done, ok := seen[newpath]; ok {
			if done != nil {
				path = *done
				continue
			}
			return pyJoin(newpath, rest), false
		}
		seen[newpath] = nil
		target, err := os.Readlink(newpath)
		if err != nil {
			path = newpath
			continue
		}
		var ok bool
		path, ok = joinRealpath(path, target, seen)
		if !ok {
			return pyJoin(path, rest), false
		}
		resolved := path
		seen[newpath] = &resolved
	}
	return path, true
}
