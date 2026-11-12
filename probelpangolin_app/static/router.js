"use strict";

// Router view: the virtual router, its controllers, crosspoints, names, faults and trace.
// Uses $, api, act, put, make, syncRows, setOptions, renderLog, renderTrace, renderGrid,
// filterRows, setHeader, statsText, clockTime and showNotice from app.js.

const RO = "/ui/api/router";
const RO_NUMBER_FIELDS = ["port", "sources", "dests", "levels", "matrix"];
const RO_SWITCHES = ["extended", "autostart"];
const RO_STATS = [["received", "Received"], ["sent", "Sent"], ["takes", "Takes"], ["refused", "Refused takes"],
  ["naks_sent", "NAK sent"], ["naks", "NAK received"], ["errors", "Broken frames"]];

let roState = null;
let roInstance = null;
let roRevision = -1;

// Fetch the router state and draw it. Skips the drawing while nothing changed.
async function roPoll() {
  try {
    const query = roInstance === null ? "" : `?since=${roRevision}&instance=${roInstance}`;
    const response = await fetch(RO + query);
    if (!response.ok) return;   // controller.js reports a lost session or server
    const data = await response.json();
    if (!data.unchanged) roRender(data);
  } catch (error) {
    // controller.js already reports an unreachable server.
  }
}

// Fetch the full state now, after a change made here.
async function roRefresh() {
  roInstance = null;
  await roPoll();
}

function roChange(changes) {
  return act(() => api(RO, changes, "PATCH"), roRefresh);
}

function roPost(path, body) {
  return act(() => api(RO + path, body || {}), roRefresh);
}

// Draw the whole Router view from a state of the server.
function roRender(data) {
  roState = data;
  roInstance = data.instance;
  roRevision = data.revision;
  const s = data.settings;
  const status = data.status;

  put("ro_bind", s.bind);
  put("ro_protocol", s.protocol);
  const old = s.protocol === "sw-p-02";
  ["ro_levels", "ro_matrix", "ro_extended", "ro-nak"].forEach((id) => { $(id).disabled = old; });
  RO_NUMBER_FIELDS.concat(RO_SWITCHES).forEach((key) => put(`ro_${key}`, s[key]));
  $("ro-start").textContent = status.running ? "Stop" : "Start";
  const count = status.clients.length;
  const state = $("ro-state");
  state.textContent = status.running
    ? `Router: ${status.description}, ${count} controller${count === 1 ? "" : "s"}` : "Router off";
  state.classList.toggle("on", status.running);
  $("ro-error").textContent = status.error;

  // Controllers and faults
  const clients = $("ro-clients");
  clients.replaceChildren(...status.clients.map((client) => {
    const item = make("li");
    item.append(client.address, make("span", "when", `  since ${clockTime(client.since)}, ${client.messages} message${client.messages === 1 ? "" : "s"}`));
    return item;
  }));
  $("ro-clients-note").hidden = count > 0;
  $("ro-clients-note").textContent = status.running ? "No controller connected." : "The router is not running.";
  $("ro-fault").disabled = !status.running;
  $("ro-kick").disabled = !status.running || count === 0;
  $("ro-fault-state").textContent = status.drop_pending || status.nak_pending
    ? `Armed: the next ${status.drop_pending} get no answer, the next ${status.nak_pending} a NAK.` : "";

  // Salvo groups that controllers have filled
  $("ro-salvos").replaceChildren(...status.salvos.map((group) => make("li", "",
    (old ? "Stored for the next GO: " : `Salvo ${group.number}: `) + group.entries.map((entry) =>
      `${old ? "" : `level ${entry.level}, `}destination ${entry.dest} from source ${entry.source}`).join(" · "))));
  $("ro-salvos-note").hidden = status.salvos.length > 0;

  const levels = Array.from({ length: status.levels }, (_, i) => [i + 1, `Level ${i + 1}`]);
  setOptions($("ro-grid-level"), levels);
  roRenderCrosspoints();

  // Source names, with the number of destinations each source is on
  const uses = new Map();
  data.destinations.forEach((dest) => dest.sources.forEach((source) => uses.set(source, (uses.get(source) || 0) + 1)));
  syncRows($("ro-sources").tBodies[0], data.sources, (source) => `s${source.number}`,
    (source) => String(uses.get(source.number) || 0), (source) => roBuildSource(source, uses.get(source.number) || 0));
  $("ro-sources-note").textContent = s.sources > data.sources.length
    ? `Showing ${data.sources.length} of ${s.sources} sources.`
    : "A new name is sent to controllers the next time they read the names.";

  if (!$("ro-pause").checked) {
    renderTrace($("ro-trace"), data.trace, $("ro-show-acks").checked, ["TX", "RX"]);
  }
  let line = statsText(status.stats, RO_STATS);
  if (status.trace_count > data.trace.length) line += ` · showing the latest ${data.trace.length} of ${status.trace_count} entries`;
  $("ro-stats").textContent = line;

  renderLog($("ro-log"), status.log, "Nothing happened yet.");
}

// Table or grid of the crosspoints.
function roRenderCrosspoints() {
  const data = roState;
  const levels = data.status.levels;
  const grid = $("ro-grid-on").checked;
  $("ro-table-wrap").hidden = grid;
  $("ro-grid-wrap").hidden = !grid;
  $("ro-grid-level-field").hidden = !grid || levels === 1;
  const rows = filterRows(data.destinations, $("ro-filter").value);
  if (grid) {
    const level = Number($("ro-grid-level").value) || 1;
    renderGrid($("ro-grid"), data.sources.slice(0, GRID_LIMIT), rows.slice(0, GRID_LIMIT), level, true,
      (source, dest) => roSet(source, dest, level));
  } else {
    setHeader($("ro-xpt"), ["Destination", "Name", "Protected"], levels, ["Last change"]);
    syncRows($("ro-xpt").tBodies[0], rows, (row) => `d${row.number}`, () => String(levels), roBuildRow);
  }
  const shown = grid ? Math.min(rows.length, GRID_LIMIT) : rows.length;
  $("ro-count").textContent = shown < data.settings.dests
    ? `Showing ${shown} of ${data.settings.dests} destinations.` : "";
}

// A text field that sends its value when it is left or Return is pressed.
function roField(value, label, role, send, numeric) {
  const input = make("input");
  input.type = numeric ? "number" : "text";
  if (numeric) input.min = "1";
  input.value = String(value);
  input.dataset.role = role;
  input.spellcheck = false;
  input.setAttribute("aria-label", label);
  input.addEventListener("change", () => { send(input.value); input.blur(); });
  return input;
}

// The cells of one destination row.
function roBuildRow(dest) {
  const name = make("td", "value");
  name.append(roField(dest.name, `Name of destination ${dest.number}`, "name",
    (value) => roPost("/name", { kind: "dest", number: dest.number, name: value })));
  const lock = make("td");
  const box = make("input");
  box.type = "checkbox";
  box.checked = dest.protected;
  box.dataset.role = "protect";
  box.setAttribute("aria-label", `Protect destination ${dest.number}`);
  box.addEventListener("change", () => roPost("/protect", { dest: dest.number, protected: box.checked }));
  lock.append(box);
  const cells = [make("td", "path", String(dest.number)), name, lock];
  dest.sources.forEach((source, index) => {
    const cell = make("td", "value number");
    cell.append(roField(source, `Source on destination ${dest.number}, level ${index + 1}`, `level${index}`,
      (value) => roSet(Number(value), dest.number, index + 1), true));
    const label = roState.sources[source - 1];
    if (label) cell.append(make("span", "shown", label.name));
    cells.push(cell);
  });
  cells.push(make("td", "path", dest.changed ? clockTime(dest.changed) : ""));
  return cells;
}

// The cells of one source row.
function roBuildSource(source, uses) {
  const name = make("td", "value");
  name.append(roField(source.name, `Name of source ${source.number}`, "name",
    (value) => roPost("/name", { kind: "source", number: source.number, name: value })));
  return [make("td", "path", String(source.number)), name, make("td", "path", uses ? String(uses) : "")];
}

// Set a crosspoint at the router.
function roSet(source, dest, level) {
  return roPost("/crosspoint", { source, dest, level, announce: $("ro-announce").checked });
}

function roBind() {
  $("ro_bind").addEventListener("change", () => roChange({ bind: $("ro_bind").value }));
  $("ro_protocol").addEventListener("change", () => roChange({ protocol: $("ro_protocol").value }));
  RO_NUMBER_FIELDS.forEach((key) => $(`ro_${key}`).addEventListener("change", () => {
    if ($(`ro_${key}`).value !== "") roChange({ [key]: Number($(`ro_${key}`).value) });
  }));
  RO_SWITCHES.forEach((key) => $(`ro_${key}`).addEventListener("change", () => roChange({ [key]: $(`ro_${key}`).checked })));
  $("ro-start").addEventListener("click", () => roPost(roState && roState.status.running ? "/stop" : "/start"));
  $("ro-fault-form").addEventListener("submit", (event) => {
    event.preventDefault();
    const old = roState && roState.settings.protocol === "sw-p-02";
    roPost("/fault", { drop: Number($("ro-drop").value) || 0, nak: old ? 0 : Number($("ro-nak").value) || 0 });
  });
  $("ro-kick").addEventListener("click", () => roPost("/disconnect-clients"));
  ["ro-filter", "ro-grid-on", "ro-grid-level"].forEach((id) => {
    $(id).addEventListener("input", () => { if (roState) roRenderCrosspoints(); });
  });
  ["ro-show-acks", "ro-pause"].forEach((id) => {
    $(id).addEventListener("change", () => { if (roState) roRender(roState); });
  });
  $("ro-clear-trace").addEventListener("click", () => roPost("/clear-trace"));
  $("ro-clear-log").addEventListener("click", () => roPost("/clear-log"));
  $("ro-to-analyzer").addEventListener("click", () => anOpenTrace(roState ? roState.trace : [],
    roState ? roState.settings.protocol : "sw-p-08"));
}

roBind();
roPoll();
setInterval(roPoll, POLL_MS);
