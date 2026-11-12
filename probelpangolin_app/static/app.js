"use strict";

// Shared by the Controller, the Router and the Analyzer view: requests to the
// server, the notice line, tables that keep their rows, the trace, the log
// and the crosspoint grid.

const POLL_MS = 400;
const UNREACHABLE = "Probel Pangolin is not reachable.";
const GRID_LIMIT = 64;        // largest number of sources and of destinations the grid shows
const TABLE_LIMIT = 256;      // rows a crosspoint table shows at once

const $ = (id) => document.getElementById(id);

// Requests that change something go out one after another, so the server
// sees them in the order they were made.
let queue = Promise.resolve();
function enqueue(task) {
  queue = queue.then(task, task);
  return queue;
}

// Send a request to the server and return its JSON answer.
//   path:   address below the page, for example "/ui/api/controller"
//   body:   data to send as JSON; without it the request is a GET
//   method: HTTP method for requests with a body, POST unless given
// The answer carries "error" if the server refused. A lost session leads to the login page.
async function api(path, body, method) {
  const options = body === undefined
    ? {}
    : { method: method || "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) };
  const response = await fetch(path, options);
  if (response.status === 401) {
    location.href = "/login";  // the session ended
    throw new Error("login required");
  }
  const data = await response.json();
  if (!response.ok && !data.error) data.error = `Request failed (${response.status})`;
  return data;
}

// Show a line at the top of the page, or hide it with an empty message.
//   info: true for a plain message, otherwise it is shown as an error
function showNotice(message, info) {
  const notice = $("notice");
  notice.textContent = message;
  notice.hidden = !message;
  notice.classList.toggle("info", Boolean(info));
}

// Run a request that changes something, report a refusal, then redraw.
//   work:    function that returns the promise of api(...)
//   refresh: function that fetches and draws the new state
// Returns the answer, or null if the server refused or cannot be reached.
function act(work, refresh) {
  return enqueue(async () => {
    try {
      const data = await work();
      showNotice(data.error || "");
      if (refresh) await refresh();
      return data.error ? null : data;
    } catch (error) {
      showNotice(UNREACHABLE);
      return null;
    }
  });
}

// Like act, but hands the answer back with its "error" instead of showing a refusal
// at the top of the page. For requests whose outcome has its own line.
function ask(work, refresh) {
  return enqueue(async () => {
    try {
      const data = await work();
      if (refresh) await refresh();
      return data;
    } catch (error) {
      return { error: UNREACHABLE };
    }
  });
}

// Seconds since 1970 as local time of day, "never" for nothing.
function clockTime(epochSeconds) {
  if (!epochSeconds) return "never";
  return new Date(epochSeconds * 1000).toLocaleTimeString([], { hour12: false });
}

// The same with milliseconds, for the trace.
function traceTime(epochSeconds) {
  const date = new Date(epochSeconds * 1000);
  return `${date.toLocaleTimeString([], { hour12: false })}.${String(date.getMilliseconds()).padStart(3, "0")}`;
}

// Make an element with a class and a text.
function make(tag, className, text) {
  const element = document.createElement(tag);
  if (className) element.className = className;
  if (text !== undefined && text !== null) element.textContent = text;
  return element;
}

// Write a value into a settings control unless the user is editing it right now.
function put(id, value) {
  const element = $(id);
  if (element.type === "checkbox") {
    element.checked = Boolean(value);
    return;
  }
  const text = value === null || value === undefined ? "" : String(value);
  if (document.activeElement !== element && element.value !== text) element.value = text;
}

// True while the user types into a field of this row. Such a row is left alone.
function isTyping(row) {
  const active = document.activeElement;
  if (!active || !row.contains(active)) return false;
  return active.tagName === "SELECT" || (active.tagName === "INPUT" && active.type !== "checkbox");
}

// Keep one table row per entry and rebuild a row only when its data changed.
//   body:  the <tbody>
//   items: the entries to show, in order
//   key:   function giving the unchanging name of an entry
//   state: function giving extra text that also forces a redraw
//   build: function (entry, row) that returns the cells of the row
function syncRows(body, items, key, state, build) {
  const known = new Map(Array.from(body.children).map((row) => [row.dataset.key, row]));
  const seen = new Set();
  let previous = null;
  items.forEach((item) => {
    const name = String(key(item));
    seen.add(name);
    let row = known.get(name);
    if (!row) {
      row = document.createElement("tr");
      row.dataset.key = name;
    }
    const signature = JSON.stringify(item) + state(item);
    if (row.dataset.signature !== signature && !isTyping(row)) {
      // A control that had the focus gets it back after the redraw.
      const active = document.activeElement;
      const role = active && row.contains(active) ? active.dataset.role : null;
      row.dataset.signature = signature;
      row.replaceChildren(...build(item, row));
      if (role) {
        const again = row.querySelector(`[data-role="${role}"]`);
        if (again) again.focus();
      }
    }
    const expected = previous ? previous.nextSibling : body.firstChild;
    if (expected !== row) body.insertBefore(row, expected);
    previous = row;
  });
  known.forEach((row, name) => { if (!seen.has(name)) row.remove(); });
}

// Fill a <select> with options unless it already holds exactly these.
//   options: list of [value, label]
function setOptions(select, options) {
  const signature = JSON.stringify(options);
  if (select.dataset.signature === signature) return;
  const chosen = select.value;
  select.dataset.signature = signature;
  select.replaceChildren(...options.map(([value, label]) => new Option(label, String(value))));
  if (options.some(([value]) => String(value) === chosen)) select.value = chosen;
}

// "5 CAM 5" for a number with a name, "5" without, a dash for nothing.
function portText(number, names) {
  if (number === null || number === undefined) return "–";
  const name = names.get(number);
  return name ? `${number}  ${name}` : String(number);
}

// Draw an activity log, newest entry first.
//   list:    the <ol>
//   entries: log entries from the server
//   empty:   text for an empty log
function renderLog(list, entries, empty) {
  const signature = entries.length ? `${entries.length}:${entries[entries.length - 1].time}` : "empty";
  if (list.dataset.signature === signature) return;
  list.dataset.signature = signature;
  list.replaceChildren();
  if (entries.length === 0) list.append(make("li", "empty", empty));
  entries.slice().reverse().forEach((entry) => {
    const item = make("li", entry.problem ? "problem-line" : "");
    item.append(make("span", "when", clockTime(entry.time)));
    item.append(make("span", "size", [entry.client, entry.action].filter(Boolean).join("  ")));
    if (entry.text) item.append(make("span", "summary", entry.text));
    list.append(item);
  });
}

// Draw a trace, newest entry first.
//   list:     the <ol>
//   rows:     trace entries from the server
//   showAcks: whether ACK and NAK lines are shown
//   sides:    what "tx" and "rx" are called here, for example ["to router", "from router"]
function renderTrace(list, rows, showAcks, sides) {
  const shown = showAcks ? rows : rows.filter((row) => row.kind !== "ack" && row.kind !== "nak");
  const signature = `${shown.length}:${shown.length ? shown[shown.length - 1].id : 0}:${showAcks}`;
  if (list.dataset.signature === signature) return;
  list.dataset.signature = signature;
  list.replaceChildren();
  if (shown.length === 0) list.append(make("li", "empty", "Nothing crossed the wire yet."));
  shown.slice().reverse().forEach((row) => {
    const item = make("li", `${row.direction}${row.problem ? " problem-line" : ""}${row.kind === "ack" ? " ack" : ""}`);
    item.append(make("span", "when", traceTime(row.time)));
    item.append(make("span", "dir", row.direction === "tx" ? sides[0] : sides[1]));
    if (row.client) item.append(make("span", "size", row.client));
    item.append(make("span", "bytes", row.hex));
    item.append(make("span", "summary", row.note ? `${row.text} (${row.note})` : row.text));
    list.append(item);
  });
}

// The crosspoint grid: one row per destination, one column per source.
//   table:        the <table>
//   sources:      [{number, name}]
//   destinations: [{number, name, sources: [per level], protected}]
//   level:        the level to show, counted from 1
//   enabled:      whether a click may switch
//   take:         function (source, destination) called on a click
function renderGrid(table, sources, destinations, level, enabled, take) {
  const signature = JSON.stringify([sources, destinations.map((d) => [d.number, d.name, d.sources[level - 1], d.protected]), level, enabled]);
  if (table.dataset.signature === signature) return;
  table.dataset.signature = signature;
  const head = make("tr");
  head.append(make("th", "corner", "Dest \\ Source"));
  sources.forEach((source) => {
    const cell = make("th", "", String(source.number));
    cell.scope = "col";
    cell.title = source.name || `Source ${source.number}`;
    head.append(cell);
  });
  const rows = destinations.map((dest) => {
    const row = make("tr", dest.protected ? "protected" : "");
    const name = make("th", "", dest.name ? `${dest.number}  ${dest.name}` : String(dest.number));
    name.scope = "row";
    row.append(name);
    const current = dest.sources[level - 1];
    sources.forEach((source) => {
      const cell = make("td");
      const button = make("button", current === source.number ? "on" : "", current === source.number ? "●" : "");
      button.type = "button";
      button.disabled = !enabled;
      button.title = `${source.name || `Source ${source.number}`} to ${dest.name || `destination ${dest.number}`}`;
      button.setAttribute("aria-label", button.title + (current === source.number ? ", connected" : ""));
      button.addEventListener("click", () => take(source.number, dest.number));
      cell.append(button);
      row.append(cell);
    });
    return row;
  });
  table.replaceChildren(head, ...rows);
}

// Rows of a crosspoint table that match the filter text, up to the limit.
function filterRows(rows, text) {
  const wanted = text.trim().toLowerCase();
  const matching = wanted
    ? rows.filter((row) => String(row.number) === wanted || row.name.toLowerCase().includes(wanted))
    : rows;
  return matching.slice(0, TABLE_LIMIT);
}

// Give the header of a crosspoint table one column per level.
//   before, after: fixed column titles around the level columns
function setHeader(table, before, levels, after) {
  const titles = [...before, ...Array.from({ length: levels }, (_, i) => (levels === 1 ? "Source" : `Level ${i + 1}`)), ...after];
  const signature = titles.join("|");
  if (table.dataset.header === signature) return;
  table.dataset.header = signature;
  const row = make("tr");
  titles.forEach((title) => { const cell = make("th", "", title); cell.scope = "col"; row.append(cell); });
  table.tHead.replaceChildren(row);
  table.tBodies[0].replaceChildren();   // rows built for another number of levels
}

// One line with the counters of a trace.
function statsText(stats, names) {
  return names.filter(([key]) => stats[key] !== undefined).map(([key, label]) => `${label} ${stats[key]}`).join(" · ");
}
