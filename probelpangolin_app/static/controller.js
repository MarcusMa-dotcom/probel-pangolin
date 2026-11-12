"use strict";

// Controller view: connection, takes, crosspoints of the router, trace and own messages.
// Uses $, api, act, put, make, syncRows, setOptions, portText, renderLog, renderTrace,
// renderGrid, filterRows, setHeader, statsText, clockTime and showNotice from app.js.

const CO = "/ui/api/controller";
const CO_TEXT_FIELDS = ["host"];
const CO_NUMBER_FIELDS = ["port", "matrix", "levels", "attempts", "destinations"];
const CO_SELECT_FIELDS = ["extended", "name_length", "protocol"];
// What SW-P-02 does not have: these controls are switched off while it is chosen.
const CO_ONLY_08 = ["co_matrix", "co_levels", "co_attempts", "co_extended", "co_name_length",
  "co_query_commands", "co_fetch_names", "co_wait_ack", "co-names", "co-commands", "co-dual",
  "co-salvo", "co-salvo-read"];
const CO_SWITCHES = ["query_commands", "fetch_names", "fetch_tally", "wait_ack", "keepalive", "auto_reconnect", "confirm"];
const CO_STATS = [["sent", "Sent"], ["received", "Received"], ["acks", "ACK"], ["naks", "NAK"],
  ["repeats", "Repeated"], ["errors", "Broken frames"]];

let coState = null;
let coInstance = null;
let coRevision = -1;
let coSourceNames = new Map();

// Fetch the controller state and draw it. Skips the drawing while nothing changed.
async function coPoll() {
  try {
    const query = coInstance === null ? "" : `?since=${coRevision}&instance=${coInstance}`;
    const response = await fetch(CO + query);
    if (response.status === 401) { location.href = "/login"; return; }
    if (!response.ok) { showNotice(response.status === 403 ? "The web interface is switched off." : UNREACHABLE); return; }
    const data = await response.json();
    if ($("notice").textContent === UNREACHABLE) showNotice("");
    if (!data.unchanged) coRender(data);
  } catch (error) {
    showNotice(UNREACHABLE);
  }
}

// Fetch the full state now, after a change made here.
async function coRefresh() {
  coInstance = null;
  await coPoll();
}

function coChange(changes) {
  return act(() => api(CO, changes, "PATCH"), coRefresh);
}

// Run a request to the router and show how it went in the line below the Take buttons.
//   path: address below the controller, for example "/route"
//   body: what to send
//   done: function (result) that returns the text for a success
async function coDo(path, body, done) {
  const line = $("co-result");
  line.textContent = "Waiting for the router...";
  line.classList.remove("error-text");
  const answer = await ask(() => api(CO + path, body), coRefresh);
  if (answer.error) {
    line.classList.add("error-text");
    line.textContent = answer.error;
    return null;
  }
  line.textContent = done(answer.result);
  return answer;
}

// Draw the whole Controller view from a state of the server.
function coRender(data) {
  coState = data;
  coInstance = data.instance;
  coRevision = data.revision;
  const s = data.settings;
  const status = data.status;

  CO_TEXT_FIELDS.concat(CO_NUMBER_FIELDS, CO_SELECT_FIELDS, CO_SWITCHES).forEach((key) => put(`co_${key}`, s[key]));

  const button = $("co-connect");
  button.textContent = status.connecting ? "Connecting..." : (status.connected || status.waiting ? "Disconnect" : "Connect");
  button.disabled = status.connecting;
  const state = $("co-state");
  state.textContent = status.connected ? `Connected: ${status.description}`
    : (status.waiting ? "Waiting for the router" : "Disconnected");
  state.classList.toggle("on", status.connected);
  $("co-error").textContent = status.error;
  const named = data.sources.length;
  $("co-info").textContent = status.connected
    ? `${status.destination_count} destination${status.destination_count === 1 ? "" : "s"} known, `
      + `${named} source name${named === 1 ? "" : "s"}`
      + (status.commands_known ? `, ${status.commands.length} commands implemented.` : ".")
    : "";

  // Take form
  coSourceNames = new Map(data.sources.map((source) => [source.number, source.name]));
  const levels = Array.from({ length: status.levels }, (_, i) => [i + 1, `Level ${i + 1}`]);
  setOptions($("co-level"), status.levels > 1 ? [["all", "All levels"], ...levels] : levels);
  setOptions($("co-grid-level"), levels);
  coDatalist($("co-source-list"), data.sources);
  coDatalist($("co-dest-list"), data.destinations);
  ["co-take", "co-ask", "co-dump", "co-names", "co-commands", "co-raw-send", "co-dual",
    "co-salvo-add", "co-salvo-go", "co-salvo-read", "co-salvo-clear"].forEach((id) => {
    $(id).disabled = !status.connected;
  });
  const old = s.protocol === "sw-p-02";
  $("co-salvo").disabled = false;
  CO_ONLY_08.forEach((id) => { if (old) $(id).disabled = true; else if (id.startsWith("co_")) $(id).disabled = false; });
  $("co-destinations-field").hidden = !old;
  $("co-old-hint").hidden = !old;

  coRenderCrosspoints();

  // Trace
  if (!$("co-pause").checked) {
    renderTrace($("co-trace"), data.trace, $("co-show-acks").checked, ["TX", "RX"]);
  }
  const stats = status.stats;
  let line = statsText(stats, CO_STATS);
  if (stats.ack_ms_last !== null) {
    line += ` · ACK after ${stats.ack_ms_last} ms (mean ${stats.ack_ms_mean}, longest ${stats.ack_ms_max}, `
      + `${stats.ack_slow} slower than 10 ms)`;
  }
  if (status.trace_count > data.trace.length) line += ` · showing the latest ${data.trace.length} of ${status.trace_count} entries`;
  $("co-stats").textContent = line;

  // Commands
  const list = $("co-command-list");
  const signature = status.commands.map((command) => command.code + command.direction).join(",");
  if (list.dataset.signature !== signature) {
    list.dataset.signature = signature;
    list.replaceChildren(...status.commands.map((command) => {
      const item = make("li");
      item.append(make("code", "", `0x${command.code.toString(16).toUpperCase().padStart(2, "0")}`), ` ${command.name.replace(/_/g, " ").toLowerCase()}`);
      if (command.direction) item.append(make("span", "when", `  ${command.direction}`));
      return item;
    }));
  }
  $("co-command-note").textContent = status.commands.length ? ""
    : (status.commands_known ? "The router reported an empty list." : "Not asked yet, or the router did not answer the question.");
  $("co-command-note").hidden = status.commands.length > 0;

  renderLog($("co-log"), status.log, "Nothing happened yet.");
}

// Offer numbers with names in the Take form.
function coDatalist(list, entries) {
  const named = entries.filter((entry) => entry.name).slice(0, 500);
  const signature = JSON.stringify(named.map((entry) => [entry.number, entry.name]));
  if (list.dataset.signature === signature) return;
  list.dataset.signature = signature;
  list.replaceChildren(...named.map((entry) => {
    const option = make("option");
    option.value = entry.name;
    option.label = `${entry.number}`;
    return option;
  }));
}

// Table or grid of the crosspoints.
function coRenderCrosspoints() {
  const data = coState;
  const status = data.status;
  const levels = status.levels;
  const grid = $("co-grid-on").checked;
  $("co-table-wrap").hidden = grid;
  $("co-grid-wrap").hidden = !grid;
  $("co-grid-level-field").hidden = !grid || levels === 1;
  const rows = filterRows(data.destinations, $("co-filter").value);
  let note = "";
  if (grid) {
    const count = Math.max(status.source_count, 1);
    const sources = Array.from({ length: Math.min(count, GRID_LIMIT) }, (_, i) => ({ number: i + 1, name: coSourceNames.get(i + 1) || "" }));
    const level = Number($("co-grid-level").value) || 1;
    renderGrid($("co-grid"), sources, rows.slice(0, GRID_LIMIT), level, status.connected,
      (source, dest) => coTake(source, dest, level));
    if (count > GRID_LIMIT || rows.length > GRID_LIMIT) note = `The grid shows up to ${GRID_LIMIT} sources and ${GRID_LIMIT} destinations. `;
    if (!data.sources.length) note += "Without names the grid has as many sources as the highest one that is routed. ";
  } else {
    setHeader($("co-xpt"), ["Destination", "Name"], levels, ["Last change", "New source", ""]);
    syncRows($("co-xpt").tBodies[0], rows, (row) => row.number,
      () => `${status.connected}${levels}${coSourceNames.size}`, coBuildRow);
  }
  $("co-count").textContent = rows.length < status.destination_count
    ? `Showing ${rows.length} of ${status.destination_count} destinations.` : "";
  if (!data.destinations.length) {
    note += status.connected
      ? "Nothing known yet. Read all crosspoints or names, or ask the router about a destination."
      : "Not connected.";
  } else if (!status.connected) {
    note += "Not connected. The table shows what was read before the connection ended.";
  } else if (!grid) {
    note += "Type a source number or name into a row and press Return. Changes made elsewhere arrive by themselves.";
  }
  $("co-xpt-note").textContent = note;
}

// The cells of one destination row.
function coBuildRow(dest) {
  const cells = [make("td", "path", String(dest.number)), make("td", "name", dest.name)];
  dest.sources.forEach((source) => cells.push(make("td", source === null ? "unknown" : "", portText(source, coSourceNames))));
  cells.push(make("td", "path", dest.changed ? clockTime(dest.changed) : ""));
  const entry = make("td", "value");
  const input = make("input");
  input.type = "text";
  input.dataset.role = "source";
  input.setAttribute("list", "co-source-list");
  input.setAttribute("aria-label", `New source for destination ${dest.number}`);
  input.spellcheck = false;
  input.disabled = !coState.status.connected;
  const go = () => {
    if (input.value.trim() === "") return;
    const value = input.value;
    input.value = "";
    input.blur();
    coTake(value, dest.number, $("co-level").value);
  };
  input.addEventListener("keydown", (event) => { if (event.key === "Enter") { event.preventDefault(); go(); } });
  entry.append(input);
  const action = make("td");
  const button = make("button", "small", "Take");
  button.type = "button";
  button.dataset.role = "take";
  button.disabled = !coState.status.connected;
  button.addEventListener("click", go);
  action.append(button);
  cells.push(entry, action);
  return cells;
}

// Route a source to a destination and say how it went.
function coTake(source, dest, level) {
  const levels = level === "all" ? "all" : Number(level) || 1;
  return coDo("/route", { source, dest, levels, confirm: $("co_confirm").checked }, (result) =>
    `Source ${result.source} is on destination ${result.dest}, level ${result.levels.join(", ")}`
    + (result.confirmed ? ", confirmed by the router." : ". Sent without waiting for the confirmation."));
}

// Show the frame that the own message would become.
let coPreviewTimer = null;
function coPreview() {
  clearTimeout(coPreviewTimer);
  coPreviewTimer = setTimeout(async () => {
    const text = $("co-raw").value.trim();
    const box = $("co-raw-preview");
    box.classList.remove("invalid");
    if (!text) {
      box.textContent = "Type the data bytes of a message: the command byte and what follows. DLE STX, byte count, checksum and DLE ETX are added.";
      return;
    }
    if ($("co-raw-mode").value === "raw") {
      box.textContent = "These bytes go out exactly as written. The router is not asked for an ACK.";
      return;
    }
    try {
      const protocol = coState ? coState.settings.protocol : "sw-p-08";
      const data = await api("/ui/api/analyzer/frame", { hex: text, protocol });
      if (data.error) {
        box.classList.add("invalid");
        box.textContent = data.error;
      } else if (protocol === "sw-p-02") {
        box.textContent = `${data.frame}\n${data.text}\nStart of message FF, checksum ${hexByte(data.checksum)}`;
      } else {
        box.textContent = `${data.frame}\n${data.text}\nByte count ${hexByte(data.byte_count)}, checksum ${hexByte(data.checksum)}`
          + (data.stuffed ? `, ${data.stuffed} DLE doubled` : "");
      }
    } catch (error) {
      box.textContent = UNREACHABLE;
    }
  }, 200);
}

const hexByte = (number) => number.toString(16).toUpperCase().padStart(2, "0");

// "in salvo 7" for SW-P-08. SW-P-02 has one store without a number.
function coSalvoName(number, word) {
  return coState.settings.protocol === "sw-p-02" ? "for the next GO" : `${word} salvo ${number}`;
}

function coBind() {
  CO_TEXT_FIELDS.forEach((key) => $(`co_${key}`).addEventListener("change", () => coChange({ [key]: $(`co_${key}`).value })));
  CO_NUMBER_FIELDS.forEach((key) => $(`co_${key}`).addEventListener("change", () => {
    if ($(`co_${key}`).value !== "") coChange({ [key]: Number($(`co_${key}`).value) });
  }));
  $("co_extended").addEventListener("change", () => coChange({ extended: $("co_extended").value }));
  $("co_protocol").addEventListener("change", () => coChange({ protocol: $("co_protocol").value }));
  $("co_name_length").addEventListener("change", () => coChange({ name_length: Number($("co_name_length").value) }));
  CO_SWITCHES.forEach((key) => $(`co_${key}`).addEventListener("change", () => coChange({ [key]: $(`co_${key}`).checked })));

  $("co-connect").addEventListener("click", () => {
    const active = coState && (coState.status.connected || coState.status.waiting);
    if (!active) {
      $("co-connect").textContent = "Connecting...";
      $("co-connect").disabled = true;
    }
    act(() => api(`${CO}/${active ? "disconnect" : "connect"}`, {}), coRefresh);
  });

  $("co-take-form").addEventListener("submit", (event) => {
    event.preventDefault();
    coTake($("co-source").value, $("co-dest").value, $("co-level").value);
  });
  $("co-ask").addEventListener("click", () => {
    const level = $("co-level").value;
    coDo("/interrogate", { dest: $("co-dest").value, level: level === "all" ? null : Number(level) }, (result) =>
      Object.entries(result).map(([lv, source]) => `Level ${lv}: source ${portText(source, coSourceNames)}`).join(" · "));
  });
  $("co-dump").addEventListener("click", () => coDo("/dump", {}, (result) =>
    Object.entries(result).map(([lv, count]) => `Level ${lv}: ${count} crosspoints read`).join(" · ")));
  $("co-names").addEventListener("click", () => coDo("/names", {}, (result) =>
    `${result.sources} source names and ${result.destinations} destination names read.`));
  $("co-commands").addEventListener("click", () => coDo("/commands", {}, (result) =>
    `The router implements ${result.length} commands.`));
  $("co-dual").addEventListener("click", () => coDo("/dual-status", {}, (result) =>
    `Dual controller status ${result.status || "without bytes"}`
    + (result.bit1 === null ? "." : `: the active/idle bit is ${result.bit1 ? "set" : "clear"}.`)));

  // Salvo group: store the take of the form, set or clear the group, read it.
  const salvo = () => Number($("co-salvo").value);
  $("co-salvo-add").addEventListener("click", () => {
    const level = $("co-level").value;
    coDo("/salvo", { action: "add", salvo: salvo(), source: $("co-source").value, dest: $("co-dest").value,
      levels: level === "all" ? "all" : Number(level) || 1 }, (result) =>
      `Stored ${coSalvoName(result.salvo, "in")}: source ${result.source} to destination ${result.dest}`
      + `${coState.status.levels > 1 ? `, level ${result.levels.join(", ")}` : ""}. Nothing is switched yet.`);
  });
  $("co-salvo-go").addEventListener("click", () => coDo("/salvo", { action: "go", salvo: salvo() }, (result) =>
    (result.outcome === "set" ? "The stored crosspoints were set."
      : "Nothing is stored, so nothing was set.")));
  $("co-salvo-clear").addEventListener("click", () => coDo("/salvo", { action: "clear", salvo: salvo() }, (result) =>
    (result.outcome === "cleared" ? "The stored crosspoints were cleared."
      : "Nothing is stored.")));
  $("co-salvo-read").addEventListener("click", () => coDo("/salvo", { action: "read", salvo: salvo() }, (result) =>
    (result.length
      ? `Salvo ${salvo()} holds: ` + result.map((entry) => `level ${entry.level}, destination ${entry.dest} from source ${entry.source}`).join(" · ")
      : `Salvo ${salvo()} holds nothing.`)));

  ["co-filter", "co-grid-on", "co-grid-level"].forEach((id) => {
    $(id).addEventListener("input", () => { if (coState) coRenderCrosspoints(); });
  });
  ["co-show-acks", "co-pause"].forEach((id) => {
    $(id).addEventListener("change", () => { if (coState) coRender(coState); });
  });
  $("co-clear-trace").addEventListener("click", () => act(() => api(`${CO}/clear-trace`, {}), coRefresh));
  $("co-clear-log").addEventListener("click", () => act(() => api(`${CO}/clear-log`, {}), coRefresh));
  $("co-to-analyzer").addEventListener("click", () => anOpenTrace(coState ? coState.trace : [],
    coState ? coState.settings.protocol : "sw-p-08"));

  $("co-raw").addEventListener("input", coPreview);
  $("co-raw-mode").addEventListener("change", coPreview);
  $("co-raw-form").addEventListener("submit", (event) => {
    event.preventDefault();
    coDo("/raw", { hex: $("co-raw").value, framed: $("co-raw-mode").value === "framed" }, (result) =>
      `Sent ${result.sent}${result.framed ? ", acknowledged by the router." : " as written."}`);
  });
}

coBind();
coPoll();
setInterval(coPoll, POLL_MS);
