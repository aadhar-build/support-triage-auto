package route

// PyJSON exports route's compact json.dumps(v, sort_keys=True) writer for the
// model package's spend log (job 070, A10). It copies no logic.
func PyJSON(v any) (string, error) { return pyjson(v) }

// ResolvePath exports route's Python Path.resolve()-like resolver for the
// model package (job 070, A10). It copies no logic.
func ResolvePath(p string) (string, error) { return resolvePython(p) }
