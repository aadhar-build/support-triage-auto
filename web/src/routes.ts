// Path to page: fetches from the Go API through fetchFn and renders HTML.
import { ApiError, getJSON } from "./api";
import {
  renderNotFound, renderOverview, renderRunMetrics, renderRuns, renderThread, renderThreadNotFound, renderThreads,
  type OverviewData, type RunMetricsData, type RunRow, type ThreadData, type ThreadsData,
} from "./pages";

type FetchFn = typeof fetch;

function is404(err: unknown): boolean {
  return err instanceof ApiError && err.status === 404;
}

// orNotFound renders the fetched value, or the given 404 page on a 404.
async function orNotFound<T>(path: string, fetchFn: FetchFn, ok: (v: T) => string, missing: () => string): Promise<string> {
  let v: T;
  try {
    v = await getJSON<T>(path, fetchFn);
  } catch (err) {
    if (is404(err)) return missing();
    throw err;
  }
  return ok(v);
}

// hasThreads is true on a 200 from the threads API, false on a 404.
async function hasThreads(run: string, fetchFn: FetchFn): Promise<boolean> {
  try {
    await getJSON<ThreadsData>(`/api/runs/${run}/threads`, fetchFn);
    return true;
  } catch (err) {
    if (is404(err)) return false;
    throw err;
  }
}

const defaultFetch: FetchFn = (input, init) => fetch(input, init);

export async function render(path: string, fetchFn: FetchFn = defaultFetch): Promise<string> {
  if (path === "/") return renderOverview(await getJSON<OverviewData>("/api/overview", fetchFn));
  if (path === "/runs") return renderRuns(await getJSON<{ runs: RunRow[] }>("/api/runs", fetchFn));
  const run = /^\/runs\/([^/]+)$/.exec(path);
  if (run && run[1]) {
    const name = run[1];
    let metrics: RunMetricsData;
    try {
      metrics = await getJSON<RunMetricsData>(`/api/runs/${name}/metrics`, fetchFn);
    } catch (err) {
      if (is404(err)) return renderNotFound();
      throw err;
    }
    return renderRunMetrics(metrics, await hasThreads(name, fetchFn));
  }
  const list = /^\/runs\/([^/]+)\/threads$/.exec(path);
  if (list && list[1]) {
    return orNotFound<ThreadsData>(`/api/runs/${list[1]}/threads`, fetchFn, renderThreads, renderNotFound);
  }
  const one = /^\/runs\/([^/]+)\/threads\/([^/]+)$/.exec(path);
  if (one && one[1] && one[2]) {
    return orNotFound<ThreadData>(`/api/runs/${one[1]}/threads/${one[2]}`, fetchFn, renderThread, renderThreadNotFound);
  }
  return renderNotFound();
}
