package admin

import (
	"os"
	"syscall"
)

// openTraceFile pins the run directory without following a link, then opens
// the trace relative to it without following a link or blocking on a FIFO.
func openTraceFile(dir string) (*os.File, error) {
	dirFD, err := syscall.Open(dir, syscall.O_RDONLY|syscall.O_DIRECTORY|syscall.O_NOFOLLOW|syscall.O_CLOEXEC, 0)
	if err != nil {
		return nil, err
	}
	defer syscall.Close(dirFD)
	fd, err := syscall.Openat(dirFD, traceFile, syscall.O_RDONLY|syscall.O_NOFOLLOW|syscall.O_NONBLOCK|syscall.O_CLOEXEC, 0)
	if err != nil {
		return nil, err
	}
	return os.NewFile(uintptr(fd), traceFile), nil
}
