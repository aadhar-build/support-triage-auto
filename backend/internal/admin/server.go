package admin

import (
	"errors"
	"net/http"
	"net/url"
	"os"
	"path/filepath"
	"regexp"
	"strings"
	"time"

	"github.com/aadhar-build/support-triage-auto/backend/internal/route"
)

// Config is what the console serves. Paths are read only.
type Config struct {
	Runs       string // folder of run subfolders
	Thresholds string // thresholds file
	Web        string // built console (index.html, assets/)
	Now        func() time.Time

	panicPath string // test hook: a request for this path panics
}

const (
	csp          = "default-src 'none'; script-src 'self'; style-src 'self'; connect-src 'self'; img-src 'self'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'"
	ctText       = "text/plain; charset=utf-8"
	ctJSON       = "application/json; charset=utf-8"
	ctHTML       = "text/html; charset=utf-8"
	msgForbidden = "forbidden: Host must be 127.0.0.1 or localhost"
	msgMethod    = "method not allowed: this console is read-only"
	msgNotFound  = "not found"
	msgInternal  = "internal error"
	msgBadTarget = "bad request"
)

var assetRE = regexp.MustCompile(`^[A-Za-z0-9][A-Za-z0-9._-]*$`)

// response is a fully built reply; nothing is written until it is complete,
// so a panic can still become a clean 500.
type response struct {
	status int
	ctype  string
	body   []byte
}

func text(status int, msg string) response {
	return response{status, ctText, []byte(msg + "\n")}
}

var errNotFound = errors.New(msgNotFound)

type handler struct{ cfg Config }

// NewHandler returns the console's single request guard and router.
func NewHandler(cfg Config) http.Handler {
	if cfg.Now == nil {
		cfg.Now = time.Now
	}
	return handler{cfg}
}

// hostOK is admin.py's _host_ok.
func hostOK(host string) bool {
	h := strings.Trim(host, " \t\n\r\f\v")
	b := []byte(h)
	for i, c := range b {
		if c >= 'A' && c <= 'Z' {
			b[i] = c + 'a' - 'A'
		}
	}
	h = string(b)
	if h == "" || strings.HasPrefix(h, "[") {
		return false
	}
	if i := strings.LastIndexByte(h, ':'); i >= 0 {
		h = h[:i]
	}
	return h == "127.0.0.1" || h == "localhost"
}

func (h handler) ServeHTTP(w http.ResponseWriter, r *http.Request) {
	w.Header().Set("X-Content-Type-Options", "nosniff")
	var res response
	switch {
	// Not origin-form (absolute, authority or *): r.Host may come from the
	// request line, so refuse before trusting it.
	case !strings.HasPrefix(r.RequestURI, "/"):
		res = text(http.StatusBadRequest, msgBadTarget)
	case !hostOK(r.Host):
		res = text(http.StatusForbidden, msgForbidden)
	case r.Method != http.MethodGet:
		res = text(http.StatusMethodNotAllowed, msgMethod)
	default:
		res = h.safeRoute(r.URL.EscapedPath())
	}
	w.Header().Set("Content-Type", res.ctype)
	if res.ctype == ctHTML {
		w.Header().Set("Content-Security-Policy", csp)
	}
	w.WriteHeader(res.status)
	_, _ = w.Write(res.body)
}

func (h handler) safeRoute(path string) (res response) {
	defer func() {
		if recover() != nil {
			res = text(http.StatusInternalServerError, msgInternal)
		}
	}()
	if h.cfg.panicPath != "" && path == h.cfg.panicPath {
		panic("test hook: " + h.cfg.Runs)
	}
	res, err := h.route(path)
	switch {
	case errors.Is(err, errNotFound):
		return text(http.StatusNotFound, msgNotFound)
	case err != nil:
		return text(http.StatusInternalServerError, msgInternal)
	}
	return res
}

func jsonResponse(v any) (response, error) {
	b, err := route.PyJSONIndent(v)
	if err != nil {
		return response{}, err
	}
	return response{http.StatusOK, ctJSON, append(b, '\n')}, nil
}

// oneSegment reports whether s is one non-empty path segment.
func oneSegment(s string) bool { return s != "" && !strings.Contains(s, "/") }

func (h handler) route(path string) (response, error) {
	switch {
	case path == "/api/runs":
		return h.apiRuns()
	case path == "/api/overview":
		o, err := Overview(h.cfg.Runs, h.cfg.Thresholds, h.cfg.Now())
		if err != nil {
			return response{}, err
		}
		return jsonResponse(o)
	case strings.HasPrefix(path, "/api/runs/") && strings.HasSuffix(path, "/metrics"):
		seg := strings.TrimSuffix(strings.TrimPrefix(path, "/api/runs/"), "/metrics")
		if !oneSegment(seg) || len("/api/runs/")+len(seg)+len("/metrics") != len(path) {
			return response{}, errNotFound
		}
		return h.apiMetrics(seg)
	case path == "/" || path == "/runs":
		return h.index()
	case strings.HasPrefix(path, "/runs/") && oneSegment(strings.TrimPrefix(path, "/runs/")):
		return h.index()
	case strings.HasPrefix(path, "/assets/"):
		return h.asset(strings.TrimPrefix(path, "/assets/"))
	}
	return response{}, errNotFound
}

func (h handler) apiRuns() (response, error) {
	runs, err := ListRuns(h.cfg.Runs)
	if err != nil {
		return response{}, err
	}
	out := make([]any, len(runs))
	for i, r := range runs {
		if r.Status != StatusOK {
			out[i] = map[string]any{"name": r.Name, "status": r.Status}
			continue
		}
		out[i] = map[string]any{
			"name": r.Name, "status": r.Status, "kind": r.Kind, "created_at": r.CreatedAt,
			"cost": map[string]any{
				"estimated_usd":           r.Cost["estimated_usd"],
				"cost_per_1000_usd":       r.Cost["cost_per_1000_usd"],
				"prices_are_placeholders": r.Cost["prices_are_placeholders"],
			},
			"has_metrics": runMetrics(r) != nil,
		}
	}
	return jsonResponse(map[string]any{"runs": out})
}

func (h handler) apiMetrics(seg string) (response, error) {
	name, err := url.PathUnescape(seg)
	if err != nil {
		return response{}, errNotFound
	}
	runs, err := ListRuns(h.cfg.Runs)
	if err != nil {
		return response{}, err
	}
	for _, r := range runs {
		if r.Name != name {
			continue
		}
		if m := runMetrics(r); m != nil {
			return jsonResponse(map[string]any{"name": r.Name, "status": StatusOK, "metrics": m})
		}
		return jsonResponse(map[string]any{"name": r.Name, "status": r.Status, "metrics": nil, "reason": NoMetrics})
	}
	return response{}, errNotFound
}

// regularFile reads p only when it is a regular file (lstat: no symlinks).
func regularFile(p string) ([]byte, error) {
	st, err := os.Lstat(p)
	if err != nil || !st.Mode().IsRegular() {
		return nil, errNotFound
	}
	b, err := os.ReadFile(p)
	if err != nil {
		return nil, errNotFound
	}
	return b, nil
}

func (h handler) index() (response, error) {
	b, err := regularFile(filepath.Join(h.cfg.Web, "index.html"))
	if err != nil {
		return response{}, err
	}
	return response{http.StatusOK, ctHTML, b}, nil
}

func (h handler) asset(file string) (response, error) {
	if !assetRE.MatchString(file) {
		return response{}, errNotFound
	}
	var ctype string
	switch {
	case strings.HasSuffix(file, ".js"):
		ctype = "text/javascript; charset=utf-8"
	case strings.HasSuffix(file, ".css"):
		ctype = "text/css; charset=utf-8"
	default:
		return response{}, errNotFound
	}
	b, err := regularFile(filepath.Join(h.cfg.Web, "assets", file))
	if err != nil {
		return response{}, err
	}
	return response{http.StatusOK, ctype, b}, nil
}
