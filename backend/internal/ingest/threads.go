package ingest

import (
	"bufio"
	"encoding/binary"
	"errors"
	"fmt"
	"io"
	"os"
	"sort"
	"time"
)

// Turn is one placed tweet of a thread.
type Turn struct {
	ID        uint64
	AuthorID  string
	Inbound   bool
	CreatedAt time.Time
	Text      string
}

// Thread is one conversation, rooted at an inbound tweet with no parent.
type Thread struct {
	RootID       uint64
	BrandIDs     []string
	Turns        []Turn
	BrandReplied bool
}

// ThreadStats is triage.threads.ThreadStats.
type ThreadStats struct {
	TweetsSeen              int
	DuplicateIDs            int
	ThreadsBuilt            int
	TweetsPlaced            int
	TweetsNotPlaced         int
	BrokenLinks             int
	Cycles                  int
	CycleTweets             int
	BrandStarted            int
	ParentLinkDisagreements int
	ResponseIDFallbacks     int
	Load                    LoadStats
}

// Options configures BuildThreads.
type Options struct {
	// TempDir holds the private index dir; empty means os.TempDir().
	TempDir string
}

// Memory: the build holds, per kept tweet, one int32 status slot in RAM; the
// tweet records (ids, links, flags, instants, row offsets; no text, no author
// ids) live in fixed-size records in temp files, sorted externally in capped
// runs. Text and author ids are re-read from the source by row offset, one
// thread at a time.

const (
	flagIRT     = 1 // node: has in_response_to; resolved: has parent
	flagInbound = 2
)

// node records, keyed by (id, row ordinal): id, seq, irt, flags, instant, offset.
const nodeSize = 41

// resolved records, sorted by id: id, parent, flags, instant, offset.
const resSize = 33

// emission records, keyed by (root instant, root id, instant, id): + offset.
const emitSize = 40

var be = binary.BigEndian

func instantKey(t time.Time) uint64 { return uint64(t.Unix()) ^ (1 << 63) }

// BuildThreads reads the twcs CSV at path and calls emit once per thread in
// (root created_at, root id) order, as threads.build_threads. emit is called
// only after the whole file is read and indexed without error.
func BuildThreads(path string, opts Options, emit func(Thread) error) (ThreadStats, error) {
	var stats ThreadStats
	base := opts.TempDir
	if base == "" {
		base = os.TempDir()
	}
	dir, err := os.MkdirTemp(base, "ingest-")
	if err != nil {
		return stats, err
	}
	defer os.RemoveAll(dir)

	src, err := os.Open(path)
	if err != nil {
		return stats, err
	}
	defer src.Close()

	b := &builder{dir: dir, stats: &stats}
	defer func() {
		if b.res != nil {
			b.res.Close()
		}
	}()
	h, load, err := b.index(src)
	stats.Load = load
	if err != nil {
		return stats, err
	}
	if err := b.resolve(); err != nil {
		return stats, err
	}
	if err := b.classify(); err != nil {
		return stats, err
	}
	return stats, b.emitThreads(src, h, emit)
}

type builder struct {
	dir    string
	stats  *ThreadStats
	nodes  *extSorter
	edges  *extSorter
	kept   string
	res    *os.File
	n      int
	status []int32
	order  *extSorter
}

func (b *builder) createFile(prefix string) (*os.File, error) {
	return os.CreateTemp(b.dir, prefix)
}

// index streams the CSV once, writing node and lister-edge records.
func (b *builder) index(src io.Reader) (header, LoadStats, error) {
	b.nodes = newExtSorter(b.dir, nodeSize, 16)
	b.edges = newExtSorter(b.dir, 24, 16)
	var seq uint64
	rec := make([]byte, nodeSize)
	edge := make([]byte, 24)
	return readTweets(src, func(t Tweet, off int64) error {
		be.PutUint64(rec[0:], t.ID)
		be.PutUint64(rec[8:], seq)
		be.PutUint64(rec[16:], t.InReplyTo)
		var fl byte
		if t.HasInReplyTo {
			fl |= flagIRT
		}
		if t.Inbound {
			fl |= flagInbound
		}
		rec[24] = fl
		be.PutUint64(rec[25:], instantKey(t.CreatedAt))
		be.PutUint64(rec[33:], uint64(off))
		if err := b.nodes.add(rec); err != nil {
			return err
		}
		for _, c := range t.ResponseIDs {
			if c == t.ID {
				continue
			}
			be.PutUint64(edge[0:], t.ID)
			be.PutUint64(edge[8:], seq)
			be.PutUint64(edge[16:], c)
			if err := b.edges.add(edge); err != nil {
				return err
			}
		}
		seq++
		return nil
	})
}

// resolve deduplicates (first row wins), filters lister edges to kept rows and
// resolves each kept tweet's parent as threads._resolve_parents.
func (b *builder) resolve() error {
	// 1. Dedup nodes into the kept file (id order, with the kept row ordinal).
	kf, err := b.createFile("kept-")
	if err != nil {
		return err
	}
	b.kept = kf.Name()
	it, err := b.nodes.iter()
	if err != nil {
		kf.Close()
		return err
	}
	w := bufio.NewWriterSize(kf, ioBuf)
	var last uint64
	first := true
	for {
		r, err := it.next()
		if err != nil {
			it.close()
			kf.Close()
			return err
		}
		if r == nil {
			break
		}
		id := be.Uint64(r)
		if !first && id == last {
			b.stats.DuplicateIDs++
			continue
		}
		first, last = false, id
		b.n++
		if _, err := w.Write(r); err != nil {
			it.close()
			kf.Close()
			return err
		}
	}
	it.close()
	if err := w.Flush(); err != nil {
		kf.Close()
		return err
	}
	if err := kf.Close(); err != nil {
		return err
	}
	b.stats.TweetsSeen = b.n

	// 2. Keep only edges listed by the kept copy; re-key by (child, lister).
	byChild := newExtSorter(b.dir, 16, 16)
	if err := b.zipKept(func(next func(uint64) ([]byte, error)) error {
		eit, err := b.edges.iter()
		if err != nil {
			return err
		}
		defer eit.close()
		out := make([]byte, 16)
		for {
			e, err := eit.next()
			if err != nil || e == nil {
				return err
			}
			k, err := next(be.Uint64(e))
			if err != nil {
				return err
			}
			if k != nil && be.Uint64(k[8:]) == be.Uint64(e[8:]) {
				be.PutUint64(out[0:], be.Uint64(e[16:]))
				be.PutUint64(out[8:], be.Uint64(e[0:]))
				if err := byChild.add(out); err != nil {
					return err
				}
			}
		}
	}); err != nil {
		return err
	}

	// 3. Merge kept nodes with their listers into the resolved file.
	rf, err := b.createFile("resolved-")
	if err != nil {
		return err
	}
	b.res = rf
	cit, err := byChild.iter()
	if err != nil {
		return err
	}
	defer cit.close()
	kin, err := os.Open(b.kept)
	if err != nil {
		return err
	}
	defer kin.Close()
	kr := bufio.NewReaderSize(kin, ioBuf)
	rw := bufio.NewWriterSize(rf, ioBuf)
	k := make([]byte, nodeSize)
	out := make([]byte, resSize)
	e, err := cit.next()
	if err != nil {
		return err
	}
	for {
		ok, err := readRec(kr, k)
		if err != nil {
			return err
		}
		if !ok {
			break
		}
		id := be.Uint64(k)
		for e != nil && be.Uint64(e) < id {
			if e, err = cit.next(); err != nil {
				return err
			}
		}
		var listers []uint64
		for e != nil && be.Uint64(e) == id {
			l := be.Uint64(e[8:])
			if len(listers) == 0 || listers[len(listers)-1] != l {
				listers = append(listers, l)
			}
			if e, err = cit.next(); err != nil {
				return err
			}
		}
		fl := k[24]
		var parent uint64
		has := false
		switch {
		case fl&flagIRT != 0:
			parent, has = be.Uint64(k[16:]), true
			for _, l := range listers {
				if l != parent {
					b.stats.ParentLinkDisagreements++
					break
				}
			}
		case len(listers) > 0:
			b.stats.ResponseIDFallbacks++
			if len(listers) > 1 {
				b.stats.ParentLinkDisagreements++
			}
			parent, has = listers[0], true
		}
		be.PutUint64(out[0:], id)
		be.PutUint64(out[8:], parent)
		out[16] = fl & flagInbound
		if has {
			out[16] |= flagIRT
		}
		copy(out[17:], k[25:41])
		if _, err := rw.Write(out); err != nil {
			return err
		}
	}
	os.Remove(b.kept)
	return rw.Flush()
}

// zipKept gives fn a cursor over the kept file: next(id) advances to the first
// kept record with id >= the argument and returns it if its id matches.
func (b *builder) zipKept(fn func(next func(uint64) ([]byte, error)) error) error {
	f, err := os.Open(b.kept)
	if err != nil {
		return err
	}
	defer f.Close()
	r := bufio.NewReaderSize(f, ioBuf)
	cur := make([]byte, nodeSize)
	have, done := false, false
	return fn(func(id uint64) ([]byte, error) {
		for !done && (!have || be.Uint64(cur) < id) {
			ok, err := readRec(r, cur)
			if err != nil {
				return nil, err
			}
			have, done = ok, !ok
		}
		if have && be.Uint64(cur) == id {
			return cur, nil
		}
		return nil, nil
	})
}

func (b *builder) readRes(i int, rec []byte) error {
	_, err := b.res.ReadAt(rec, int64(i)*resSize)
	return err
}

// find binary-searches the id-sorted resolved file.
func (b *builder) find(id uint64) (int, bool, error) {
	var buf [8]byte
	lo, hi := 0, b.n
	for lo < hi {
		mid := int(uint(lo+hi) >> 1)
		if _, err := b.res.ReadAt(buf[:], int64(mid)*resSize); err != nil {
			return 0, false, err
		}
		v := be.Uint64(buf[:])
		switch {
		case v == id:
			return mid, true, nil
		case v < id:
			lo = mid + 1
		default:
			hi = mid
		}
	}
	return 0, false, nil
}

// classify maps every kept tweet to its root as threads._classify. status:
// 0 unknown, -1 not placed, <=-2 on the current walk at -(s+2), >0 root idx+1.
func (b *builder) classify() error {
	b.status = make([]int32, b.n)
	var path []int
	rec := make([]byte, resSize)
	for start := 0; start < b.n; start++ {
		if b.status[start] != 0 {
			continue
		}
		path = path[:0]
		cur := start
		var result int32
		for {
			s := b.status[cur]
			if s > 0 || s == -1 {
				result = s
				break
			}
			if s <= -2 {
				pos := int(-(s + 2))
				b.stats.Cycles++
				b.stats.CycleTweets += len(path) - pos
				for _, p := range path[pos:] {
					b.status[p] = -1
				}
				path = path[:pos]
				result = -1
				break
			}
			b.status[cur] = int32(-(len(path) + 2))
			path = append(path, cur)
			if err := b.readRes(cur, rec); err != nil {
				return err
			}
			if rec[16]&flagIRT == 0 {
				if rec[16]&flagInbound != 0 {
					result = int32(cur + 1)
				} else {
					b.stats.BrandStarted++
					result = -1
				}
				break
			}
			p, ok, err := b.find(be.Uint64(rec[8:]))
			if err != nil {
				return err
			}
			if !ok {
				b.stats.BrokenLinks++
				result = -1
				break
			}
			cur = p
		}
		for _, p := range path {
			b.status[p] = result
		}
	}

	b.order = newExtSorter(b.dir, emitSize, 32)
	out := make([]byte, emitSize)
	root := make([]byte, resSize)
	for i, s := range b.status {
		if s <= 0 {
			continue
		}
		b.stats.TweetsPlaced++
		r := int(s - 1)
		if r == i {
			b.stats.ThreadsBuilt++
		}
		if err := b.readRes(i, rec); err != nil {
			return err
		}
		if err := b.readRes(r, root); err != nil {
			return err
		}
		copy(out[0:8], root[17:25])
		copy(out[8:16], root[0:8])
		copy(out[16:24], rec[17:25])
		copy(out[24:32], rec[0:8])
		copy(out[32:40], rec[25:33])
		if err := b.order.add(out); err != nil {
			return err
		}
	}
	b.stats.TweetsNotPlaced = b.stats.TweetsSeen - b.stats.TweetsPlaced
	b.status = nil
	return nil
}

// rowReader re-parses one record of the source at a byte offset.
type rowReader struct {
	src *os.File
	cr  *csvReader
	h   header
}

func (rr *rowReader) turn(id uint64, off int64) (Turn, error) {
	rr.cr.br.Reset(io.NewSectionReader(rr.src, off, 1<<62))
	rr.cr.off = off
	rec, _, err := rr.cr.next()
	if err != nil {
		return Turn{}, fmt.Errorf("re-read row at offset %d: %w", off, err)
	}
	t, why, err := parseRow(rec, rr.h)
	if err != nil || why != "" || t.ID != id {
		return Turn{}, errors.Join(errors.New("re-read row differs from the indexed row"), err)
	}
	return Turn{ID: t.ID, AuthorID: t.AuthorID, Inbound: t.Inbound, CreatedAt: t.CreatedAt, Text: t.Text}, nil
}

func (b *builder) emitThreads(src *os.File, h header, emit func(Thread) error) error {
	it, err := b.order.iter()
	if err != nil {
		return err
	}
	defer it.close()
	rr := &rowReader{src: src, cr: newCSVReader(src, 4096), h: h}
	var th *Thread
	flush := func() error {
		if th == nil {
			return nil
		}
		seen := map[string]bool{}
		th.BrandIDs = []string{}
		for _, tu := range th.Turns {
			if !tu.Inbound && !seen[tu.AuthorID] {
				seen[tu.AuthorID] = true
				th.BrandIDs = append(th.BrandIDs, tu.AuthorID)
			}
		}
		sort.Strings(th.BrandIDs)
		th.BrandReplied = len(th.BrandIDs) > 0
		err := emit(*th)
		th = nil
		return err
	}
	for {
		r, err := it.next()
		if err != nil {
			return err
		}
		if r == nil {
			return flush()
		}
		rootID := be.Uint64(r[8:])
		if th != nil && th.RootID != rootID {
			if err := flush(); err != nil {
				return err
			}
		}
		if th == nil {
			th = &Thread{RootID: rootID}
		}
		tu, err := rr.turn(be.Uint64(r[24:]), int64(be.Uint64(r[32:])))
		if err != nil {
			return err
		}
		th.Turns = append(th.Turns, tu)
	}
}
