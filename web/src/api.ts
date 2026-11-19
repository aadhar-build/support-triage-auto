// Same-origin JSON fetches from the Go console API.
export class ApiError extends Error {
  constructor(readonly status: number) {
    super(`HTTP ${status}`);
  }
}

export async function getJSON<T>(path: string, fetchFn: typeof fetch = fetch): Promise<T> {
  if (!path.startsWith("/api/") || path.startsWith("//")) {
    throw new Error("only same-origin /api/ paths are fetched");
  }
  const res = await fetchFn(path, { headers: { Accept: "application/json" } });
  if (!res.ok) throw new ApiError(res.status);
  return (await res.json()) as T;
}
