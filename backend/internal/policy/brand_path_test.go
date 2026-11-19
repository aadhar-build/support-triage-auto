package policy

import (
	"errors"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

func brandErr(t *testing.T, name, dir string) string {
	t.Helper()
	p, err := BrandPath(name, dir)
	var be *BrandError
	if !errors.As(err, &be) {
		t.Fatalf("BrandPath(%q) = %q, %v (%T), want *BrandError", name, p, err, err)
	}
	return err.Error()
}

func TestBrandPathNames(t *testing.T) {
	root := atRepoRoot(t)
	realRoot, err := filepath.EvalSymlinks(root)
	if err != nil {
		t.Fatal(err)
	}
	got, err := BrandPath("alpha", "tests/fixtures/brands")
	if want := realRoot + "/tests/fixtures/brands/alpha/guidelines.md"; err != nil || got != want {
		t.Errorf("BrandPath(alpha) = %q, %v; want %q", got, err, want)
	}
	missing := filepath.Join(t.TempDir(), "made-up-brands-dir-that-does-not-exist")
	// Python 3.12 reprs, with surrogateescape for the invalid byte.
	for name, repr := range map[string]string{
		"../x":                  `'../x'`,
		"Alpha":                 `'Alpha'`,
		"a/b":                   `'a/b'`,
		"":                      `''`,
		strings.Repeat("a", 65): "'" + strings.Repeat("a", 65) + "'",
		".hidden":               `'.hidden'`,
		"-x":                    `'-x'`,
		"a\xffb":                `'a\udcffb'`,
		"it's":                  `"it's"`,
	} {
		want := "bad brand name " + repr + "; expected [a-z0-9][a-z0-9_-]{0,63}"
		if got := brandErr(t, name, missing); got != want {
			t.Errorf("BrandPath(%q) error %q, want %q", name, got, want)
		}
	}
	if got, want := brandErr(t, "gamma", "tests/fixtures/brands"), "brand 'gamma' not found under "+realRoot+"/tests/fixtures/brands"; got != want {
		t.Errorf("gamma: %q, want %q", got, want)
	}
}

func mkfile(t *testing.T, p string) {
	t.Helper()
	if err := os.MkdirAll(filepath.Dir(p), 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(p, []byte("x"), 0o644); err != nil {
		t.Fatal(err)
	}
}

func symlink(t *testing.T, target, link string) {
	t.Helper()
	if err := os.MkdirAll(filepath.Dir(link), 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.Symlink(target, link); err != nil {
		t.Fatal(err)
	}
}

func realTemp(t *testing.T) string {
	t.Helper()
	tmp, err := filepath.EvalSymlinks(t.TempDir())
	if err != nil {
		t.Fatal(err)
	}
	return tmp
}

func TestBrandPathSymlinks(t *testing.T) {
	tmp := realTemp(t)
	brands := tmp + "/brands"
	mkfile(t, tmp+"/outside/evil/guidelines.md")
	mkfile(t, brands+"/real/guidelines.md")
	symlink(t, tmp+"/outside/evil", brands+"/dirlink")
	symlink(t, tmp+"/outside/evil/guidelines.md", brands+"/filelink/guidelines.md")
	symlink(t, brands+"/real", brands+"/inlink")
	symlink(t, "..", brands+"/rootlink/guidelines.md") // resolves to brands itself
	mkfile(t, tmp+"/brands-evil/x/guidelines.md")
	symlink(t, "../../brands-evil/x/guidelines.md", brands+"/evil/guidelines.md")
	symlink(t, brands, tmp+"/brandslink")

	for _, name := range []string{"dirlink", "filelink", "rootlink", "evil"} {
		if got, want := brandErr(t, name, brands), "brand '"+name+"' resolves outside "+brands; got != want {
			t.Errorf("%s: %q, want %q", name, got, want)
		}
	}
	for _, c := range []struct{ name, dir string }{
		{"inlink", brands},
		{"real", tmp + "/missing/../brands"},
		{"inlink", tmp + "/brandslink"},
	} {
		got, err := BrandPath(c.name, c.dir)
		if want := brands + "/real/guidelines.md"; err != nil || got != want {
			t.Errorf("BrandPath(%q, %q) = %q, %v; want %q", c.name, c.dir, got, err, want)
		}
	}
	// A relative brands dir resolves against the working directory.
	t.Chdir(tmp)
	got, err := BrandPath("real", "missing/../brandslink")
	if want := brands + "/real/guidelines.md"; err != nil || got != want {
		t.Errorf("relative: %q, %v; want %q", got, err, want)
	}
}

func TestBrandPathSymlinkLoop(t *testing.T) {
	tmp := realTemp(t)
	brands := tmp + "/brands"
	symlink(t, "b", brands+"/a/guidelines.md")
	symlink(t, "guidelines.md", brands+"/a/b")
	symlink(t, tmp+"/loop/x", brands+"/c/guidelines.md")
	symlink(t, "y", tmp+"/loop/x")
	symlink(t, "x", tmp+"/loop/y")
	symlink(t, "l2", tmp+"/loopdir/l1")
	symlink(t, "l1", tmp+"/loopdir/l2")

	for _, c := range []struct{ name, dir, root string }{
		{"a", brands, brands},
		{"c", brands, brands},
		{"a", tmp + "/loopdir/l1", tmp + "/loopdir/l1"},
	} {
		want := "brand '" + c.name + "' resolves through a symlink loop under " + c.root
		if got := brandErr(t, c.name, c.dir); got != want {
			t.Errorf("%s under %s: %q, want %q", c.name, c.dir, got, want)
		}
	}
	var out, errb strings.Builder
	code := BrandRulesMain([]string{"--conformance", "--brand", "a", "--rules", tmp + "/none.json", "--brands-dir", brands}, &out, &errb)
	if want := "error: brand 'a' resolves through a symlink loop under " + brands + "\n"; out.String() != "" || errb.String() != want || code != 2 {
		t.Errorf("CLI: got (%q, %q, %d), want stderr %q and 2", out.String(), errb.String(), code, want)
	}
}
