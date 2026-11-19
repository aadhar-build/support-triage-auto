/// <reference types="vite/client" />
import "./style.css";
import { ApiError, getJSON } from "./api";
import {
  renderError, renderNotFound, renderOverview, renderRunMetrics, renderRuns,
  type OverviewData, type RunMetricsData, type RunRow,
} from "./pages";

async function render(path: string): Promise<string> {
  if (path === "/") return renderOverview(await getJSON<OverviewData>("/api/overview"));
  if (path === "/runs") return renderRuns(await getJSON<{ runs: RunRow[] }>("/api/runs"));
  const m = /^\/runs\/([^/]+)$/.exec(path);
  if (m && m[1]) {
    try {
      return renderRunMetrics(await getJSON<RunMetricsData>(`/api/runs/${m[1]}/metrics`));
    } catch (err) {
      if (err instanceof ApiError && err.status === 404) return renderNotFound();
      throw err;
    }
  }
  return renderNotFound();
}

const app = document.getElementById("app");
if (app) {
  render(location.pathname).then(
    (html) => { app.innerHTML = html; },
    () => { app.innerHTML = renderError(); },
  );
}
