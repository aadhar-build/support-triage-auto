package ingest

import (
	"bufio"
	"bytes"
	"container/heap"
	"io"
	"os"
	"sort"
)

// runCap caps the bytes of records one sorter holds in memory before it spills
// a sorted run to disk.
const runCap = 2 << 20

const ioBuf = 32 << 10

// extSorter is an external merge sort of fixed-size records ordered by
// bytes.Compare on their first key bytes. Keys must be unique.
type extSorter struct {
	dir       string
	size, key int
	buf       []byte
	runs      []string
}

func newExtSorter(dir string, size, key int) *extSorter {
	return &extSorter{dir: dir, size: size, key: key}
}

func (s *extSorter) add(rec []byte) error {
	if s.buf == nil {
		s.buf = make([]byte, 0, runCap/s.size*s.size)
	}
	s.buf = append(s.buf, rec...)
	if len(s.buf)+s.size > cap(s.buf) {
		return s.spill()
	}
	return nil
}

type recSlice struct {
	b         []byte
	size, key int
	tmp       []byte
}

func (r recSlice) Len() int { return len(r.b) / r.size }
func (r recSlice) Less(i, j int) bool {
	return bytes.Compare(r.b[i*r.size:i*r.size+r.key], r.b[j*r.size:j*r.size+r.key]) < 0
}
func (r recSlice) Swap(i, j int) {
	a, b := r.b[i*r.size:(i+1)*r.size], r.b[j*r.size:(j+1)*r.size]
	copy(r.tmp, a)
	copy(a, b)
	copy(b, r.tmp)
}

func (s *extSorter) spill() error {
	if len(s.buf) == 0 {
		return nil
	}
	sort.Sort(recSlice{b: s.buf, size: s.size, key: s.key, tmp: make([]byte, s.size)})
	f, err := os.CreateTemp(s.dir, "run-")
	if err != nil {
		return err
	}
	s.runs = append(s.runs, f.Name())
	w := bufio.NewWriterSize(f, ioBuf)
	if _, err := w.Write(s.buf); err != nil {
		f.Close()
		return err
	}
	if err := w.Flush(); err != nil {
		f.Close()
		return err
	}
	s.buf = s.buf[:0]
	return f.Close()
}

// mergeIter pulls records in key order from every spilled run.
type mergeIter struct {
	s     *extSorter
	files []*os.File
	h     runHeap
	cur   []byte
}

type runHead struct {
	r   *bufio.Reader
	rec []byte
}

type runHeap struct {
	items []*runHead
	key   int
}

func (h runHeap) Len() int { return len(h.items) }
func (h runHeap) Less(i, j int) bool {
	return bytes.Compare(h.items[i].rec[:h.key], h.items[j].rec[:h.key]) < 0
}
func (h runHeap) Swap(i, j int) { h.items[i], h.items[j] = h.items[j], h.items[i] }
func (h *runHeap) Push(x any)   { h.items = append(h.items, x.(*runHead)) }
func (h *runHeap) Pop() any {
	x := h.items[len(h.items)-1]
	h.items = h.items[:len(h.items)-1]
	return x
}

// iter spills what is buffered and starts the merge. The caller must close it.
func (s *extSorter) iter() (*mergeIter, error) {
	if err := s.spill(); err != nil {
		return nil, err
	}
	s.buf = nil
	it := &mergeIter{s: s, h: runHeap{key: s.key}, cur: make([]byte, s.size)}
	for _, name := range s.runs {
		f, err := os.Open(name)
		if err != nil {
			it.close()
			return nil, err
		}
		it.files = append(it.files, f)
		rh := &runHead{r: bufio.NewReaderSize(f, ioBuf), rec: make([]byte, s.size)}
		ok, err := readRec(rh.r, rh.rec)
		if err != nil {
			it.close()
			return nil, err
		}
		if ok {
			it.h.items = append(it.h.items, rh)
		}
	}
	heap.Init(&it.h)
	return it, nil
}

func readRec(r io.Reader, rec []byte) (bool, error) {
	_, err := io.ReadFull(r, rec)
	if err == io.EOF {
		return false, nil
	}
	return err == nil, err
}

// next returns the next record (valid until the following call), or nil at the end.
func (it *mergeIter) next() ([]byte, error) {
	if it.h.Len() == 0 {
		return nil, nil
	}
	top := it.h.items[0]
	copy(it.cur, top.rec)
	ok, err := readRec(top.r, top.rec)
	if err != nil {
		return nil, err
	}
	if ok {
		heap.Fix(&it.h, 0)
	} else {
		heap.Pop(&it.h)
	}
	return it.cur, nil
}

func (it *mergeIter) close() {
	for _, f := range it.files {
		f.Close()
	}
	for _, name := range it.s.runs {
		os.Remove(name)
	}
	it.s.runs = nil
}
