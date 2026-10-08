// Fills a page of ../pages from its .json twin: the URL the page was served at, with ".json" added,
// returns what it shows.
"use strict";

const CLASSES = ["defined_state", "undef_flags_only", "exception_mismatch", "crash"];
const X87_POINTERS = ["x87_dp", "x87_ip", "x87_opcode"];

const CLASS_LABEL = {
  defined_state: "defined state",
  undef_flags_only: "undefined flags only",
  exception_mismatch: "exception",
  crash: "crash",
};

// Second encoding next to the cell colour, so that the class never rests on hue alone.
const CLASS_MARK = {
  defined_state: "",
  undef_flags_only: "u",
  exception_mismatch: "e",
  crash: "c",
};

const STATUS_TEXT = {
  unsupported: ["–", "the CPU lacks a feature this instruction needs"],
  notrun: ["·", "not yet run"],
  running: ["…", "the run is partway through this"],
};

const GROUP_LABEL = {
  match: "matches the reference",
  running: "run in progress here",
  notrun: "not yet run",
  unsupported: "instruction not supported",
};

// Bit names of the flag registers, low bit first; [name, shift, width] for multi-bit fields.
const FLAG_BITS = {
  flag: [["CF", 0], ["PF", 2], ["AF", 4], ["ZF", 6], ["SF", 7], ["TF", 8], ["IF", 9], ["DF", 10],
         ["OF", 11], ["NT", 14], ["RF", 16], ["VM", 17], ["AC", 18], ["ID", 21]],
  x87_status: [["IE", 0], ["DE", 1], ["ZE", 2], ["OE", 3], ["UE", 4], ["PE", 5], ["SF", 6],
               ["ES", 7], ["C0", 8], ["C1", 9], ["C2", 10], ["TOP", 11, 3], ["C3", 14], ["B", 15]],
  x87_control: [["IM", 0], ["DM", 1], ["ZM", 2], ["OM", 3], ["UM", 4], ["PM", 5], ["PC", 8, 2],
                ["RC", 10, 2], ["X", 12]],
  mxcsr: [["IE", 0], ["DE", 1], ["ZE", 2], ["OE", 3], ["UE", 4], ["PE", 5], ["DAZ", 6], ["IM", 7],
          ["DM", 8], ["ZM", 9], ["OM", 10], ["UM", 11], ["PM", 12], ["RC", 13, 2], ["FZ", 15]],
};

// ── Formatting helpers ───────────────────────────────────────────────

const $ = (id) => document.getElementById(id);

const ESCAPES = { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#x27;" };

const esc = (s) => String(s).replace(/[&<>"']/g, (c) => ESCAPES[c]);

const num = (n) => n.toLocaleString("en-US");

const classLabel = (k) => CLASS_LABEL[k] ?? k;

const isInt = (v) => typeof v == "bigint" || Number.isInteger(v);

// Entries of a {name: count} object, largest count first.
const byCount = (counts) => Object.entries(counts).sort((a, b) => b[1] - a[1]);

function hex(v) {
  return v < 0 ? `-0x${(-v).toString(16)}` : `0x${v.toString(16)}`;
}

function decodeFlags(key, value) {
  const bits = FLAG_BITS[key];
  if (!bits || !isInt(value)) {
    return null;
  }
  const v = BigInt(value);
  const out = [];
  for (const [name, shift, width] of bits) {
    if (width) {
      out.push(`${name}=${(v >> BigInt(shift)) & ((1n << BigInt(width)) - 1n)}`);
    } else if ((v >> BigInt(shift)) & 1n) {
      out.push(name);
    }
  }
  return out.join(" ") || "(none set)";
}

function fmtValue(key, v) {
  if (v == null) {
    return "<span class='mut'>absent</span>";
  }
  if (!isInt(v)) {
    const text = typeof v == "string" ? v : JSON.stringify(v, (k, x) => (typeof x == "bigint" ? String(x) : x));
    return esc(text);
  }
  const bits = decodeFlags(key, v);
  return hex(v) + (bits ? `<br><span class='bits'>${esc(bits)}</span>` : "");
}

function fmtShare(x) {
  if (x == null) {
    return "?";
  }
  const p = 100 * x;
  if (p >= 99.95) {
    return "100%";
  }
  if (p >= 10) {
    return `${p.toFixed(0)}%`;
  }
  if (p >= 1) {
    return `${p.toFixed(1)}%`;
  }
  if (p >= 0.01) {
    return `${p.toFixed(2)}%`;
  }
  return "&lt;0.01%";
}

// Depth of a cell's colour, 1 to 4.
function bucket(x) {
  if (x == null) {
    return 4;
  }
  return x < 0.001 ? 1 : x < 0.01 ? 2 : x < 0.1 ? 3 : 4;
}

// ── Filter ───────────────────────────────────────────────────────────

// The classes and diff keys switched off, as the page's data reports them.
function filterOf(d) {
  return { xc: d.filter.hidden_classes, xk: d.filter.hidden_keys };
}

const NO_FILTER = { xc: [], xk: [] };

// The filter as a query string, with a leading "?" (empty when there is nothing to say).
function query(flt, extra = {}) {
  const parts = [];
  for (const [name, off] of [["xc", flt.xc], ["xk", flt.xk]]) {
    if (off.length) {
      parts.push(`${name}=${[...off].sort().map(encodeURIComponent).join(",")}`);
    }
  }
  for (const [k, v] of Object.entries(extra)) {
    if (v != null && v !== "" && v !== false) {
      parts.push(`${k}=${encodeURIComponent(v)}`);
    }
  }
  return parts.length ? `?${parts.join("&")}` : "";
}

function filterBox(kind, name, n, off) {
  return [
    `<label><input type='checkbox' data-${kind}='${esc(name)}'${off ? "" : " checked"}>`,
    `${esc(kind == "xc" ? classLabel(name) : name)} <span class='mut'>${num(n)}</span></label>`,
  ].join("");
}

function classBoxes(inventory, flt) {
  return CLASSES.filter((k) => k in inventory.classes || flt.xc.includes(k))
    .map((k) => filterBox("xc", k, inventory.classes[k] ?? 0, flt.xc.includes(k)))
    .join("");
}

function keyBoxes(inventory, flt) {
  const names = Object.keys(inventory.keys);
  names.push(...flt.xk.filter((k) => !(k in inventory.keys)).sort());
  return names.map((k) => filterBox("xk", k, inventory.keys[k] ?? 0, flt.xk.includes(k))).join("");
}

// Class and diff-key toggles. A checked box is a class or key that counts; unchecking one reloads
// the page with it in xc= or xk=.
function showFilters(d, flt, extra = {}) {
  const path = location.pathname;
  const form = $("filters");
  $("classes").innerHTML = classBoxes(d.inventory, flt);
  $("keys").innerHTML = keyBoxes(d.inventory, flt);
  $("keys-box").open = flt.xk.length > 0;
  $("ignored").textContent = flt.xk.length ? ` (${flt.xk.length} ignored)` : "";
  const noX87 = { xc: flt.xc, xk: [...new Set([...flt.xk, ...X87_POINTERS])] };
  $("no-x87").href = path + query(noX87, extra);
  $("everything").href = path + query(NO_FILTER, extra);

  form.addEventListener("change", () => {
    const off = (kind) =>
      [...form.querySelectorAll(`input[data-${kind}]`)].filter((b) => !b.checked).map((b) => b.dataset[kind]);
    location.href = path + query({ xc: off("xc"), xk: off("xk") }, extra);
  });
}

// ── Page parts, each returning an HTML string ────────────────────────

function cellTd(c, modelLabel, states) {
  if (c == null) {
    // run, and nothing differs (or nothing to run: an ERR case)
    return states ? "<td class='z'>0%</td>" : "<td></td>";
  }
  if (!("n" in c)) {
    const [text, why] = STATUS_TEXT[c.status];
    return `<td class='st' title='${esc(modelLabel)}: ${esc(why)}'>${text}</td>`;
  }
  const classes = byCount(c.classes);
  const top = classes[0][0];
  const tip = [`${modelLabel}: ${num(c.n)} states`];
  tip.push(...classes.map(([k, n]) => `${classLabel(k)}: ${num(n)}`));
  const keys = byCount(c.keys);
  if (keys.length) {
    const more = keys.length > 12 ? `, +${keys.length - 12} more` : "";
    tip.push(`keys: ${keys.slice(0, 12).map(([k, n]) => `${k} ${num(n)}`).join(", ")}${more}`);
  }
  if (c.status in STATUS_TEXT) {
    tip.push(STATUS_TEXT[c.status][1]);
  }
  const mark = CLASS_MARK[top] ?? "?";
  return [
    `<td class='c k-${esc(top)} b${bucket(c.share)}${c.status ? " part" : ""}' title='${esc(tip.join("\n"))}'>`,
    `${fmtShare(c.share)}${mark ? `<sup>${mark}</sup>` : ""}</td>`,
  ].join("");
}

// The launch years of the models under a band: "2019", or "2017–2019" when they differ.
function years(band) {
  const known = band.years.filter((y) => y != null);
  if (!known.length) {
    return "";
  }
  const from = Math.min(...known);
  const to = Math.max(...known);
  return from == to ? `${from}` : `${from}–${to}`;
}

// Two header rows: bands of vendor and microarchitecture with their years, then one column per model.
function modelHead(models, first) {
  const bands = [];
  const cols = [];
  let prev = null;
  for (const m of models) {
    const key = `${m.vendor}\n${m.uarch}`;
    const edge = key != prev;
    if (edge) {
      bands.push({ vendor: m.vendor, uarch: m.uarch, years: [] });
      prev = key;
    }
    bands.at(-1).years.push(m.year);
    const prog = m.done ? "" : `<span class='prog'>${(100 * m.progress).toFixed(0)}%</span>`;
    const run = m.done ? "run finished" : `run at ${(100 * m.progress).toFixed(1)}%: counts are partial`;
    const year = m.year ? ` (${m.year})` : "";
    const tip = `${m.cpu_model}\n${m.vendor} ${m.uarch}${year}, ${m.clusters.join(", ")}\n${run}`;
    cols.push(`<th class='model${edge ? " edge" : ""}' title='${esc(tip)}'><span>${esc(m.label)}</span>${prog}</th>`);
  }
  const top = bands.map(
    (b) => `<th colspan='${b.years.length}' class='band' title='${esc(b.vendor)}'>${esc(b.uarch)}<small>${years(b)}</small></th>`,
  );
  return `<tr><th rowspan='2' class='corner'>${first}</th>${top.join("")}</tr><tr>${cols.join("")}</tr>`;
}

function tableRows(d, rowHead) {
  return d.rows
    .map((r) => {
      const tds = d.models.map((m) => cellTd(r.cells[m.cpu_model], m.label, r.states));
      return `<tr>${rowHead(r)}${tds.join("")}</tr>`;
    })
    .join("");
}

function rowHead(name, href, states) {
  return [
    `<th class='row' data-name='${esc(name.toLowerCase())}'>`,
    `<a href='${esc(href)}'>${esc(name)}</a><span class='mut'>${num(states)}</span></th>`,
  ].join("");
}

function banner(cov) {
  const covers = `Summary covers ${cov.percent.toFixed(1)}% of results`;
  if (cov.paused) {
    return `The summary job is paused. ${covers}.`;
  }
  return cov.caught_up ? "" : `${covers}; counts are partial.`;
}

function instructionLink(mnemonic, flt) {
  return `<a href='${esc(`/i/${encodeURIComponent(mnemonic)}${query(flt)}`)}'>${esc(mnemonic)}</a>`;
}

function testCaseAbout(d) {
  const doc = d.url ? ` · <a href='${esc(d.url)}' rel='noreferrer'>documentation</a>` : "";
  return `test case ${d.test_case_id} · opcode <code>${esc(d.opcode)}</code> · ${num(d.states)} states${doc}`;
}

function groupWhat(g) {
  if (!g.n) {
    return GROUP_LABEL[g.kind] ?? g.kind;
  }
  const b = g.breakdown ?? { classes: {}, keys: {} };
  const classes = byCount(b.classes).map(([k, n]) => `${classLabel(k)} ${num(n)}`);
  const keys = byCount(b.keys).map(([k, n]) => `${esc(k)} ${num(n)}`);
  const note = g.kind == "mismatch" ? "" : ` <span class='mut'>(${GROUP_LABEL[g.kind]})</span>`;
  return `${num(g.n)} states: ${classes.join(", ")}${keys.length ? `; keys ${keys.join(", ")}` : ""}${note}`;
}

function groupRow(g) {
  return `<tr><th>${g.n ? `G${g.id}` : ""}</th><td>${esc(g.models.join(", "))}</td><td>${groupWhat(g)}</td></tr>`;
}

function gridCell(c, flt) {
  if (c == null) {
    return "<td></td>";
  }
  let text;
  if (c.class == "exception_mismatch") {
    text = `exception ${esc(c.got_exception_kind || "none")}, expected ${esc(c.expected_exception_kind || "none")}`;
  } else if (c.class == "crash") {
    text = "crash";
  } else {
    text = c.diff_keys.map((k) => (flt.xk.includes(k) ? `<span class='mut'>${esc(k)}</span>` : esc(k))).join(" ");
  }
  const mark = CLASS_MARK[c.class] ?? "?";
  return `<td class='g k-${esc(c.class)}'>${text}${mark ? `<sup>${mark}</sup>` : ""}</td>`;
}

function gridRow(r, shown, flt) {
  const path = `${location.pathname}/${r.state_index}`;
  const cells = shown.map((g) => gridCell(r.groups[g.id], flt));
  return `<tr><th><a href='${path}'>${r.state_index}</a></th>${cells.join("")}</tr>`;
}

// The rows of the state table: the exception when there is one, then one row per key.
function stateRows(d, mism) {
  const initial = d.initial_state ?? {};
  const expected = d.expected_final_state ?? {};
  const rows = [];
  if (d.expected_exception_kind || mism.some((g) => g.exception_kind)) {
    const cells = mism.map((g) => {
      const diff = g.exception_kind != d.expected_exception_kind;
      return `<td class='${diff ? "diff" : ""}'>${esc(g.exception_kind || "none")}</td>`;
    });
    rows.push(`<tr><th>exception</th><td></td><td>${esc(d.expected_exception_kind || "none")}</td>${cells.join("")}</tr>`);
  }
  for (const k of d.keys) {
    const cells = mism.map((g) => {
      if (g.final_state == null) {
        return "<td class='mut'>no state</td>";
      }
      return `<td class='${g.diff_keys.includes(k) ? "diff" : ""}'>${fmtValue(k, g.final_state[k])}</td>`;
    });
    rows.push(
      [
        `<tr><th>${esc(k)}</th>`,
        `<td>${Object.hasOwn(initial, k) ? fmtValue(k, initial[k]) : ""}</td>`,
        `<td>${fmtValue(k, expected[k])}</td>`,
        `${cells.join("")}</tr>`,
      ].join(""),
    );
  }
  return rows.join("");
}

function checkRow(c) {
  return [
    `<tr><th>${esc(c.name)}</th>`,
    `<td class='${c.ok ? "" : "diff"}'>${c.ok ? "ok" : "FAILED"}</td>`,
    `<td>${esc(c.checked_at)}</td>`,
    `<td><pre>${esc(JSON.stringify(c.detail, null, 1))}</pre></td></tr>`,
  ].join("");
}

// ── Views ────────────────────────────────────────────────────────────

// What every page has: the title, the trail after the home link, the banner.
function frame(title, cov, flt = NO_FILTER, crumbs = []) {
  document.title = `${title} - x86db explorer`;
  $("home").href = `/${query(flt)}`;
  $("crumbs").innerHTML = crumbs.map((c) => ` <span class='mut'>/</span> ${c}`).join("");
  const text = banner(cov);
  $("banner").textContent = text;
  $("banner").hidden = !text;
}

// The table of the matrix and of an instruction: rows, the search box and the "show all" link.
function showTable(d, flt, what, total, first, head) {
  const path = location.pathname;
  showFilters(d, flt, d.show_all ? { all: 1 } : {});
  $("shown").textContent = `${d.rows.length} of ${total} ${what} shown.`;
  $("other").href = path + query(flt, d.show_all ? {} : { all: 1 });
  $("other").textContent = d.show_all ? `hide ${what} without mismatch` : `show all ${total}`;
  $("head").innerHTML = modelHead(d.models, first);
  $("rows").innerHTML = tableRows(d, head);

  const rows = [...$("rows").children].map((tr) => [tr, tr.firstElementChild.dataset.name]);
  $("find").addEventListener("input", () => {
    const q = $("find").value.trim().toLowerCase();
    for (const [tr, name] of rows) {
      tr.hidden = q != "" && !name.includes(q);
    }
  });
}

function matrix(d) {
  const flt = filterOf(d);
  const q = query(flt);
  frame("Matrix", d.coverage, flt);
  showTable(d, flt, "instructions", d.instructions_total, "instruction <span class='mut'>states</span>", (r) =>
    rowHead(r.instruction, `/i/${encodeURIComponent(r.instruction)}${q}`, r.states),
  );
}

function instruction(d) {
  const flt = filterOf(d);
  const q = query(flt);
  frame(d.name, d.coverage, flt, [esc(d.name)]);
  $("name").textContent = d.name;
  $("doc").hidden = !d.url;
  $("doc").href = d.url ?? "";
  showTable(d, flt, "test cases", d.test_cases_total, "test case <span class='mut'>states</span>", (r) =>
    rowHead(r.instruction, `/tc/${r.test_case_id}${q}`, r.states),
  );
}

function testCase(d) {
  const flt = filterOf(d);
  const path = location.pathname;
  frame(d.instruction, d.coverage, flt, d.mnemonic ? [instructionLink(d.mnemonic, flt)] : []);
  $("insn").textContent = d.instruction;
  $("about").innerHTML = testCaseAbout(d);
  showFilters(d, flt);
  $("groups").innerHTML = d.groups.map(groupRow).join("");

  // The state grid has one column per group that has mismatches.
  const shown = d.groups.filter((g) => g.n);
  $("states").hidden = !shown.length;
  $("none").hidden = shown.length > 0;
  const heads = shown.map((g) => `<th title='${esc(g.models.join(", "))}'>G${g.id}</th>`);
  $("grid-head").innerHTML = `<th>state</th>${heads.join("")}`;
  $("grid").innerHTML = d.grid.map((r) => gridRow(r, shown, flt)).join("");
  $("first").hidden = d.after < 0;
  $("first").href = path + query(flt);
  $("more").hidden = d.next_after == null;
  $("more").href = path + query(flt, { after: d.next_after });
  $("more").textContent = `states after ${d.next_after}`;
}

function state(d) {
  const path = location.pathname;
  const crumbs = d.mnemonic ? [instructionLink(d.mnemonic, NO_FILTER)] : [];
  crumbs.push(`<a href='/tc/${d.test_case_id}'>test case ${d.test_case_id}</a>`, `state ${d.state_index}`);
  frame(`${d.instruction} state ${d.state_index}`, d.coverage, NO_FILTER, crumbs);
  $("insn").textContent = d.instruction;
  $("about").innerHTML = testCaseAbout(d);
  $("state").textContent = `State ${d.state_index}`;
  $("toggle").href = d.show_all ? path : `${path}?all=1`;
  $("toggle").textContent = d.show_all ? "only differing keys" : "all keys";
  $("json").href = `${path}.json`;

  // One column per group of models that got the same values; the other groups are listed below.
  const mism = d.groups.filter((g) => g.kind == "mismatch");
  const heads = mism.map((g) => `<th>G${g.id}<br><span class='mut'>${esc(g.models.join(", "))}</span></th>`);
  const rows = stateRows(d, mism);
  $("values").hidden = !rows;
  $("none").hidden = Boolean(rows);
  $("state-head").innerHTML = `<th>key</th><th>initial</th><th>expected</th>${heads.join("")}`;
  $("state-rows").innerHTML = rows;
  $("others").innerHTML = d.groups
    .filter((g) => g.kind != "mismatch")
    .map((g) => `<li>${esc(GROUP_LABEL[g.kind])}: ${esc(g.models.join(", "))}</li>`)
    .join("");
  $("inexact").hidden = EXACT_INTEGERS;
}

function checks(d) {
  const cov = d.coverage;
  frame("Checks", cov);
  $("watermark").textContent =
    `Summary watermark at result id ${num(cov.result_id)} of ${num(cov.tip_id)} (${cov.percent.toFixed(2)}%).`;
  $("checks").innerHTML = d.checks.map(checkRow).join("");
  $("none").hidden = d.checks.length > 0;
}

const VIEWS = { matrix, instruction, test_case: testCase, state, checks };

// ── Loading ──────────────────────────────────────────────────────────

// Registers are 64 bits wide and a JSON number is only exact up to 2^53: integers beyond that are
// read from the text of the response as BigInt, where the browser gives access to it.
let EXACT_INTEGERS = false;
JSON.parse("0", (key, value, context) => {
  EXACT_INTEGERS = context !== undefined;
});

function exact(key, value, context) {
  if (typeof value == "number" && !Number.isSafeInteger(value) && context && /^-?\d+$/.test(context.source)) {
    return BigInt(context.source);
  }
  return value;
}

async function load() {
  const path = location.pathname == "/" ? "/matrix" : location.pathname;
  try {
    const response = await fetch(`${path}.json${location.search}`);
    if (!response.ok) {
      throw new Error(response.status == 404 ? "not found" : `HTTP ${response.status}`);
    }
    VIEWS[document.body.dataset.view](JSON.parse(await response.text(), exact));
    $("page").hidden = false;
  } catch (err) {
    $("error").textContent = `Could not load this page: ${err.message}`;
    $("error").hidden = false;
  }
}

load();
