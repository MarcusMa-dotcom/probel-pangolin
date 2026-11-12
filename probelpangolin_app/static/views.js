"use strict";

// The views of the page and the buttons that switch between them. Loaded last.

const VIEWS = ["controller", "router", "analyzer", "settings", "about"];

// Switch to one of the views and remember it in the address.
function showView(name) {
  if (!VIEWS.includes(name)) name = "controller";
  VIEWS.forEach((view) => {
    $(`view-${view}`).hidden = view !== name;
    $(`tab-${view}`).setAttribute("aria-pressed", String(view === name));
  });
  if (location.hash !== `#${name}`) history.replaceState(null, "", `#${name}`);
  if (name === "settings") loadAccess();
}

const viewFromAddress = () => showView(location.hash.slice(1));
VIEWS.forEach((view) => $(`tab-${view}`).addEventListener("click", () => showView(view)));
window.addEventListener("hashchange", viewFromAddress);
viewFromAddress();
