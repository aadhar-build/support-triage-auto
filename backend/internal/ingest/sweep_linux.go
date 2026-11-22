//go:build linux

package ingest

import (
	"crypto/rand"
	"encoding/hex"
	"io"
	"os"
	"strings"
	"syscall"
	"time"
	"unsafe"
)

var acquireLease = func(dir string, cfg sweepConfig) *os.File {
	f, err := os.OpenFile(dir+"/.lease", os.O_CREATE|os.O_EXCL|os.O_RDWR, 0600)
	if err != nil {
		return nil
	}
	if cfg.afterLeaseCreate != nil {
		cfg.afterLeaseCreate()
	}
	if syscall.Flock(int(f.Fd()), syscall.LOCK_EX|syscall.LOCK_NB) != nil {
		f.Close()
		return nil
	}
	if cfg.afterLock != nil {
		cfg.afterLock()
	}
	if _, err = f.WriteString(leaseMarker); err != nil {
		// Retain the lock even if a partial write happened.
		return f
	}
	return f
}

const oPath = 0x200000
const atRemovedir = 0x200

func unlinkAt(fd int, name string, flags int) error {
	p, err := syscall.BytePtrFromString(name)
	if err != nil {
		return err
	}
	_, _, e := syscall.Syscall(syscall.SYS_UNLINKAT, uintptr(fd), uintptr(unsafe.Pointer(p)), uintptr(flags))
	if e != 0 {
		return e
	}
	return nil
}

// openNoMount uses openat2's NO_XDEV restriction, including bind mounts.
// Unsupported kernels fail closed. The hook permits testing refusal without mounts.
var openNoMount = func(fd int, name string, flags int) (int, error) {
	p, err := syscall.BytePtrFromString(name)
	if err != nil {
		return -1, err
	}
	how := struct{ Flags, Mode, Resolve uint64 }{uint64(flags), 0, 0x01}
	n, _, e := syscall.Syscall6(437, uintptr(fd), uintptr(unsafe.Pointer(p)), uintptr(unsafe.Pointer(&how)), unsafe.Sizeof(how), 0, 0)
	if e != 0 {
		return -1, e
	}
	return int(n), nil
}

// checkTree verifies every descendant before any deletion.
func checkTree(fd int) error {
	es, err := entries(fd)
	if err != nil {
		return err
	}
	for _, e := range es {
		n, err := openNoMount(fd, e.Name(), oPath|syscall.O_NOFOLLOW|syscall.O_CLOEXEC)
		if err != nil {
			return err
		}
		var st syscall.Stat_t
		err = syscall.Fstat(n, &st)
		syscall.Close(n)
		if err != nil {
			return err
		}
		if st.Mode&syscall.S_IFMT == syscall.S_IFDIR {
			d, err := openNoMount(fd, e.Name(), syscall.O_RDONLY|syscall.O_DIRECTORY|syscall.O_NOFOLLOW|syscall.O_CLOEXEC)
			if err != nil {
				return err
			}
			var ds syscall.Stat_t
			err = syscall.Fstat(d, &ds)
			if err == nil && same(st, ds) {
				err = checkTree(d)
			} else if err == nil {
				err = syscall.EINVAL
			}
			syscall.Close(d)
			if err != nil {
				return err
			}
		}
	}
	return nil
}

func openStat(fd int, name string) (int, syscall.Stat_t, error) {
	var st syscall.Stat_t
	n, err := syscall.Openat(fd, name, oPath|syscall.O_NOFOLLOW|syscall.O_CLOEXEC, 0)
	if err != nil {
		return -1, st, err
	}
	if err = syscall.Fstat(n, &st); err != nil {
		syscall.Close(n)
		return -1, st, err
	}
	return n, st, nil
}
func same(a, b syscall.Stat_t) bool { return a.Dev == b.Dev && a.Ino == b.Ino }
func eligible(st syscall.Stat_t, now time.Time) bool {
	return st.Mode&syscall.S_IFMT == syscall.S_IFDIR && st.Uid == uint32(os.Geteuid()) && st.Mode&0077 == 0 && now.Sub(time.Unix(st.Mtim.Sec, st.Mtim.Nsec)) > 24*time.Hour
}
func digits(s string, max int) bool {
	if len(s) == 0 || (max > 0 && len(s) > max) {
		return false
	}
	for _, c := range s {
		if c < '0' || c > '9' {
			return false
		}
	}
	return true
}
func legacyName(s string) bool {
	return strings.HasPrefix(s, "ingest-") && digits(strings.TrimPrefix(s, "ingest-"), 10)
}
func entries(fd int) ([]os.DirEntry, error) {
	n, err := syscall.Openat(fd, ".", syscall.O_RDONLY|syscall.O_DIRECTORY|syscall.O_CLOEXEC, 0)
	if err != nil {
		return nil, err
	}
	f := os.NewFile(uintptr(n), "dir")
	defer f.Close()
	return f.ReadDir(-1)
}
func legacyContents(fd int) bool {
	es, err := entries(fd)
	if err != nil {
		return false
	}
	for _, e := range es {
		valid := false
		for _, p := range []string{"run-", "kept-", "resolved-"} {
			if strings.HasPrefix(e.Name(), p) && digits(strings.TrimPrefix(e.Name(), p), 0) {
				valid = true
			}
		}
		if !valid {
			return false
		}
		n, st, err := openStat(fd, e.Name())
		if err != nil {
			return false
		}
		syscall.Close(n)
		if st.Mode&syscall.S_IFMT != syscall.S_IFREG {
			return false
		}
	}
	return true
}
func sweepTemp(base string, cfg sweepConfig) {
	fd, err := syscall.Open(base, syscall.O_RDONLY|syscall.O_DIRECTORY|syscall.O_NOFOLLOW|syscall.O_CLOEXEC, 0)
	if err != nil {
		return
	}
	defer syscall.Close(fd)
	var bst syscall.Stat_t
	if syscall.Fstat(fd, &bst) != nil {
		return
	}
	es, err := entries(fd)
	if err != nil {
		return
	}
	now := time.Now()
	if cfg.now != nil {
		now = cfg.now()
	}
	for _, e := range es {
		name := e.Name()
		isNew := strings.HasPrefix(name, scratchPrefix)
		legacy := cfg.legacy && legacyName(name) && bst.Uid == uint32(os.Geteuid()) && bst.Mode&0077 == 0
		if !isNew && !legacy {
			continue
		}
		sweepCandidate(fd, name, isNew, now, cfg)
	}
}
func sweepCandidate(base int, name string, isNew bool, now time.Time, cfg sweepConfig) {
	p, st, err := openStat(base, name)
	if err != nil {
		return
	}
	defer syscall.Close(p)
	if !eligible(st, now) {
		return
	}
	d, err := openNoMount(base, name, syscall.O_RDONLY|syscall.O_DIRECTORY|syscall.O_NOFOLLOW|syscall.O_CLOEXEC)
	if err != nil {
		return
	}
	defer syscall.Close(d)
	var ds syscall.Stat_t
	if syscall.Fstat(d, &ds) != nil || !same(st, ds) {
		return
	}
	if isNew {
		l, err := syscall.Openat(d, ".lease", syscall.O_RDONLY|syscall.O_NOFOLLOW|syscall.O_NONBLOCK|syscall.O_CLOEXEC, 0)
		if err != nil {
			return
		}
		f := os.NewFile(uintptr(l), "lease")
		defer f.Close()
		var ls syscall.Stat_t
		if syscall.Fstat(l, &ls) != nil || ls.Mode&syscall.S_IFMT != syscall.S_IFREG || syscall.Flock(l, syscall.LOCK_EX|syscall.LOCK_NB) != nil {
			return
		}
		data, err := io.ReadAll(io.LimitReader(f, int64(len(leaseMarker)+1)))
		if err != nil || string(data) != leaseMarker {
			return
		}
	} else if !legacyContents(d) {
		return
	}
	if checkTree(d) != nil {
		return
	}
	if cfg.beforeMove != nil && cfg.beforeMove() {
		return
	}
	var stage string
	for i := 0; i < 10; i++ {
		var b [16]byte
		if _, err := rand.Read(b[:]); err != nil {
			return
		}
		stage = ".support-triage-ingest-sweep-" + hex.EncodeToString(b[:])
		err = syscall.Mkdirat(base, stage, 0700)
		if err == nil {
			break
		}
		if err != syscall.EEXIST {
			return
		}
		stage = ""
	}
	if stage == "" {
		return
	}
	s, err := syscall.Openat(base, stage, syscall.O_RDONLY|syscall.O_DIRECTORY|syscall.O_NOFOLLOW|syscall.O_CLOEXEC, 0)
	if err != nil {
		return
	}
	defer syscall.Close(s)
	var ss syscall.Stat_t
	es, err := entries(s)
	if err != nil || syscall.Fstat(s, &ss) != nil || ss.Uid != uint32(os.Geteuid()) || ss.Mode&0777 != 0700 || len(es) != 0 {
		return
	}
	if syscall.Renameat(base, name, s, "victim") != nil {
		_ = unlinkAt(base, stage, atRemovedir)
		return
	}
	v, vs, err := openStat(s, "victim")
	if err != nil {
		return
	}
	syscall.Close(v)
	if !same(st, vs) || !eligible(vs, now) {
		return
	}
	if checkTree(d) != nil || removeContents(d) != nil {
		return
	}
	if unlinkAt(s, "victim", atRemovedir) != nil {
		return
	}
	_ = unlinkAt(base, stage, atRemovedir)
}
func removeContents(fd int) error {
	es, err := entries(fd)
	if err != nil {
		return err
	}
	for _, e := range es {
		n, err := openNoMount(fd, e.Name(), oPath|syscall.O_NOFOLLOW|syscall.O_CLOEXEC)
		if err != nil {
			return err
		}
		var st syscall.Stat_t
		err = syscall.Fstat(n, &st)
		syscall.Close(n)
		if err != nil {
			return err
		}
		if st.Mode&syscall.S_IFMT == syscall.S_IFDIR {
			d, err := openNoMount(fd, e.Name(), syscall.O_RDONLY|syscall.O_DIRECTORY|syscall.O_NOFOLLOW|syscall.O_CLOEXEC)
			if err != nil {
				return err
			}
			var ds syscall.Stat_t
			err = syscall.Fstat(d, &ds)
			if err == nil && same(st, ds) {
				err = removeContents(d)
			} else if err == nil {
				err = syscall.EINVAL
			}
			syscall.Close(d)
			if err != nil {
				return err
			}
			if err = unlinkAt(fd, e.Name(), atRemovedir); err != nil {
				return err
			}
		} else if err = unlinkAt(fd, e.Name(), 0); err != nil {
			return err
		}
	}
	return nil
}
