/// <reference types="vite/client" />
import "./style.css";
import { renderError } from "./pages";
import { render } from "./routes";

const app = document.getElementById("app");
if (app) {
  render(location.pathname).then(
    (html) => { app.innerHTML = html; },
    () => { app.innerHTML = renderError(); },
  );
}
