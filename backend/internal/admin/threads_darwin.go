package admin

import (
	"os"
	"path/filepath"
	"syscall"
)

// oNoFollowAny is O_NOFOLLOW_ANY from Apple's sys/fcntl.h: the kernel refuses
// a symlink in any component of the path. syscall does not export it.
const oNoFollowAny = 0x20000000

// openTraceFile resolves links only in the configured runs root (which may sit
// under a system link such as /var) and leaves the run folder and trace name
// for the kernel to check at open.
func openTraceFile(dir string) (*os.File, error) {
	root, err := filepath.EvalSymlinks(filepath.Dir(dir))
	if err != nil {
		return nil, err
	}
	path := filepath.Join(root, filepath.Base(dir), traceFile)
	return os.OpenFile(path, os.O_RDONLY|syscall.O_NONBLOCK|syscall.O_CLOEXEC|oNoFollowAny, 0)
}
