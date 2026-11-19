import { escapeHtml } from "./escape";

const app = document.getElementById("app");
if (app) {
  app.innerHTML = `<h1>${escapeHtml("Triage console")}</h1><p>Pages arrive with the port issues.</p>`;
}
