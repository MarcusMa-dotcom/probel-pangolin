"use strict";

// Analyzer view: reads captured bytes and builds messages. Nothing here needs a connection.
// Uses $, api, ask, make, showNotice and UNREACHABLE from app.js, hexByte from controller.js.

const AN = "/ui/api/analyzer";
const AN_EXAMPLE = [
  "10 02 02 01 00 02 04 05 F2 10 03",          // connect: level 2, destination 3 <- source 5
  "10 06",                                     // ACK
  "10 02 04 01 00 02 04 05 F0 10 03",          // connected
  "10 06",
  "10 02 02 00 00 10 10 10 10 05 D9 10 03",    // destination 17 <- source 17: 0x10 is doubled
  "10 06",
  "10 02 01 00 00 02 04 99 10 03",             // wrong checksum
  "10 15",                                     // NAK
].join("\n");
const AN_EXAMPLE_02 = [
  "FF 02 00 02 04 78",       // connect: destination 3 <- source 5
  "FF 04 00 02 04 76",       // connected
  "FF 01 00 02 7D",          // interrogate destination 3
  "FF 03 00 02 04 77",       // tally
  "FF 05 00 01 06 74",       // connect on go: destination 2 <- source 7
  "FF 0C 00 01 06 6D",       // its acknowledge
  "FF 06 00 7A",             // go
  "FF 0D 00 73",             // go done
  "FF 03 00 02 04 70",       // wrong checksum
].join("\n");
const anProtocol = () => $("an-protocol").value;
const anOld = () => anProtocol() === "sw-p-02";
// Which fields a message needs: level, destination, source, name length.
const AN_FIELDS = {
  connect: ["level", "dest", "source"],
  interrogate: ["level", "dest"],
  tally_dump: ["level"],
  source_names: ["level", "length"],
  dest_names: ["length"],
  commands: [],
  salvo_connect: ["level", "dest", "source", "salvo"],
  salvo_go: ["salvo"],
  salvo_clear: ["salvo"],
  salvo_interrogate: ["salvo", "index"],
  dual_status: [],
};

// Read the bytes in the text area and list what they hold.
async function anDecode() {
  const summary = $("an-summary");
  const table = $("an-rows");
  summary.classList.remove("error-text");
  let data;
  try {
    data = await api(`${AN}/decode`, { hex: $("an-capture").value, protocol: anProtocol() });
  } catch (error) {
    data = { error: UNREACHABLE };
  }
  if (data.error) {
    summary.classList.add("error-text");
    summary.textContent = data.error;
    table.hidden = true;
    return;
  }
  const parts = [`${data.bytes} bytes`, `${data.messages} message${data.messages === 1 ? "" : "s"}`];
  if (data.protocol !== "sw-p-02") parts.push(`${data.acks} ACK`, `${data.naks} NAK`);
  parts.push(`${data.errors} ${data.protocol === "sw-p-02" ? "not readable" : "broken"}`);
  summary.textContent = parts.join(" · ") + (data.truncated ? ` · only the first ${data.rows.length} entries are listed` : "");
  table.tBodies[0].replaceChildren(...data.rows.map((row) => {
    const tr = make("tr", row.problem ? "problem-row" : "");
    tr.append(make("td", "path", String(row.offset)), make("td", "text bytes", row.hex),
      make("td", "details", row.text), make("td", "details", row.detail));
    return tr;
  }));
  table.hidden = false;
}

// Put the frames of a trace into the text area, oldest first, and read them.
function anOpenTrace(rows, protocol) {
  if (protocol && protocol !== anProtocol()) {
    $("an-protocol").value = protocol;
    anProtocolChanged();
  }
  // A broken message is kept in the trace without its start byte: put it back for SW-P-02.
  $("an-capture").value = rows.map((row) => (protocol === "sw-p-02" && row.kind === "error" ? `FF ${row.hex}` : row.hex)).join("\n");
  showView("analyzer");
  if (rows.length) anDecode();
}

// Show only the fields the chosen message needs.
function anShowFields() {
  let wanted = AN_FIELDS[$("an-kind").value] || [];
  // SW-P-02 has one level, no matrix and no numbered salvo groups.
  if (anOld()) wanted = wanted.filter((name) => name === "dest" || name === "source");
  ["level", "dest", "source", "length", "salvo", "index"].forEach((name) => {
    $(`an-${name}-field`).hidden = !wanted.includes(name);
  });
  // Salvo groups and the dual controller status exist in the standard commands only.
  const kind = $("an-kind").value;
  $("an-extended-field").hidden = anOld() || kind.startsWith("salvo") || kind === "dual_status" || kind === "commands";
  if ($("an-extended-field").hidden) $("an-extended").checked = false;
  $("an-matrix-field").hidden = anOld() || !["connect", "interrogate", "tally_dump", "source_names", "dest_names", "salvo_connect"].includes(kind);
}

// The protocol was changed: offer its messages and read the capture again.
function anProtocolChanged() {
  const select = $("an-kind");
  const kinds = JSON.parse(anOld() ? select.dataset.kindsOld : select.dataset.kinds);
  setOptions(select, kinds);   // pairs of key and title, in the order of the program
  anShowFields();
  $("an-payload").value = "";
  $("an-frame").textContent = "Build a message or type its data bytes to see the frame.";
  $("an-frame").dataset.frame = "";
  $("an-capture").placeholder = anOld() ? "FF 02 00 02 04 78 FF 04 00 02 04 76" : "10 02 02 01 00 02 04 05 F2 10 03 10 06";
}

// Show the frame for the data bytes in the field.
let anFrameTimer = null;
function anFrame() {
  clearTimeout(anFrameTimer);
  anFrameTimer = setTimeout(async () => {
    const box = $("an-frame");
    const text = $("an-payload").value.trim();
    box.classList.remove("invalid");
    box.dataset.frame = "";
    if (!text) {
      box.textContent = "Build a message or type its data bytes to see the frame.";
      return;
    }
    let data;
    try {
      data = await api(`${AN}/frame`, { hex: text, protocol: anProtocol() });
    } catch (error) {
      data = { error: UNREACHABLE };
    }
    if (data.error) {
      box.classList.add("invalid");
      box.textContent = data.error;
      return;
    }
    box.dataset.frame = data.frame;
    if (data.protocol === "sw-p-02") {
      box.textContent = `${data.frame}\n${data.text}\n`
        + `Start of message FF · command ${data.command} · ${data.byte_count} message byte${data.byte_count === 1 ? "" : "s"} · `
        + `checksum ${hexByte(data.checksum)} (7 bits)`;
      return;
    }
    box.textContent = `${data.frame}\n${data.text}\n`
      + `DLE STX · ${data.byte_count} data byte${data.byte_count === 1 ? "" : "s"} · byte count ${hexByte(data.byte_count)} · `
      + `checksum ${hexByte(data.checksum)} · DLE ETX`
      + (data.stuffed ? ` · ${data.stuffed} byte${data.stuffed === 1 ? "" : "s"} equal to DLE sent twice` : "")
      + (data.oversize ? "\nLonger than the 128 data bytes that every system is guaranteed to accept." : "");
  }, 200);
}

function anBind() {
  $("an-decode-form").addEventListener("submit", (event) => { event.preventDefault(); anDecode(); });
  $("an-example").addEventListener("click", () => {
    $("an-capture").value = anOld() ? AN_EXAMPLE_02 : AN_EXAMPLE;
    anDecode();
  });
  $("an-protocol").addEventListener("change", () => {
    anProtocolChanged();
    if ($("an-capture").value.trim()) anDecode();
  });
  $("an-clear").addEventListener("click", () => {
    $("an-capture").value = "";
    $("an-summary").textContent = "";
    $("an-rows").hidden = true;
  });
  $("an-kind").addEventListener("change", anShowFields);
  $("an-payload").addEventListener("input", anFrame);
  $("an-build-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    const note = $("an-build-note");
    note.classList.remove("error-text");
    let data;
    try {
      data = await api(`${AN}/build`, {
        kind: $("an-kind").value,
        protocol: anProtocol(),
        matrix: Number($("an-matrix").value),
        level: Number($("an-level").value),
        dest: Number($("an-dest").value),
        source: Number($("an-source").value),
        name_length: Number($("an-length").value),
        salvo: Number($("an-salvo").value),
        index: Number($("an-index").value),
        extended: $("an-extended").checked,
      });
    } catch (error) {
      data = { error: UNREACHABLE };
    }
    if (data.error) {
      note.classList.add("error-text");
      note.textContent = data.error;
      return;
    }
    note.textContent = "Matrix, level, source and destination count from 1, as on a panel. On the wire they count from 0. Salvo groups and their indexes are 0 to 127 as they are.";
    $("an-payload").value = data.payload;
    anFrame();
  });
  $("an-send").addEventListener("click", async () => {
    const note = $("an-build-note");
    note.classList.remove("error-text");
    if (coState && coState.settings.protocol !== anProtocol()) {
      note.classList.add("error-text");
      note.textContent = "The controller is set to the other protocol. Choose the same one in the Controller tab.";
      return;
    }
    const answer = await ask(() => api("/ui/api/controller/raw", { hex: $("an-payload").value, framed: true }), coRefresh);
    note.classList.toggle("error-text", Boolean(answer.error));
    note.textContent = answer.error || `Sent ${answer.result.sent}. The answer is in the trace of the controller.`;
  });
  $("an-to-capture").addEventListener("click", () => {
    const frame = $("an-frame").dataset.frame;
    if (!frame) return;
    const area = $("an-capture");
    area.value = area.value.trim() ? `${area.value.trim()}\n${frame}` : frame;
    anDecode();
  });
  anShowFields();
}

anBind();
