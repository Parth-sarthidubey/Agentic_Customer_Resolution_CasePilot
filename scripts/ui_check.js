/* Headless render check for both CasePilot UIs against a running server.
 *
 * The pytest suite proves the agents reach the right state; it says nothing about whether the
 * pages that show that state actually render. This loads both UIs in jsdom, fails on any script
 * error, and asserts the things a demo depends on: the board draws, the triage question comes
 * back with tappable choices, and the desk shows what the customer was offered.
 *
 *   uv run uvicorn app.main:app --port 8011      # in one terminal
 *   npm i jsdom && node scripts/ui_check.js      # in another
 *   BASE=http://localhost:8000 node scripts/ui_check.js
 */
const { JSDOM, VirtualConsole } = require("jsdom");

const BASE = process.env.BASE || "http://127.0.0.1:8011";

async function load(path, { waitMs = 3500 } = {}) {
  const errors = [];
  const vc = new VirtualConsole();
  vc.on("jsdomError", (e) => errors.push(e.message + (e.detail ? " :: " + e.detail : "")));
  vc.on("error", (...a) => errors.push("console.error: " + a.join(" ")));

  const html = await (await fetch(BASE + path)).text();
  const dom = new JSDOM(html, {
    url: BASE + path,
    runScripts: "dangerously",
    resources: "usable",
    pretendToBeVisual: true,
    virtualConsole: vc,
    // jsdom has no fetch, and page scripts run during construction - so it has to be installed
    // before parsing, not after.
    beforeParse(window) {
      window.fetch = (u, o) => fetch(new URL(u, BASE).href, o);
      window.scrollTo = () => {};
      window.alert = (m) => errors.push("alert: " + m);
    },
  });
  await new Promise((r) => setTimeout(r, waitMs));
  return { dom, doc: dom.window.document, errors };
}

const results = [];
function check(name, ok, detail = "") {
  results.push({ name, ok, detail });
  console.log(`${ok ? "  PASS" : "  FAIL"}  ${name}${detail && !ok ? "  <- " + detail : ""}`);
}

(async () => {
  console.log("\n=== CUSTOMER PORTAL (/portal) ===");
  {
    const { doc, errors } = await load("/portal");
    check("no JS errors", errors.length === 0, errors.join(" | "));
    check("customer selector populated", doc.querySelectorAll("#me option").length > 0);
    check("request list rendered", doc.querySelectorAll("#list .req").length > 0,
          `${doc.querySelectorAll("#list .req").length} requests`);
    const thread = doc.querySelector("#thread");
    check("thread or welcome rendered", !!thread && thread.innerHTML.trim().length > 0);
    check("composer present", !!doc.querySelector("#msg") && !!doc.querySelector("#send"));
  }

  console.log("\n=== PORTAL: triage chips on a waiting case ===");
  {
    // open a deliberately vague chat as a customer with two recent orders
    
    const body = new URLSearchParams();
    body.set("message", "one of my recent things turned up broken");
    body.set("customer_email", "sofia.berg@example.com");
    body.set("customer_name", "Sofia Berg");
    const r = await fetch(BASE + "/api/chat", {
      method: "POST",
      headers: { "Content-Type": "application/x-www-form-urlencoded" },
      body,
    });
    const c = await r.json();
    console.log(`  (filed ${c.number}, waiting for triage…)`);
    await new Promise((res) => setTimeout(res, 9000));

    const { doc, errors } = await load("/portal", { waitMs: 4000 });
    check("no JS errors", errors.length === 0, errors.join(" | "));
    // the portal lists only the signed-in customer's requests - switch to Sofia first
    const sel = doc.querySelector("#me");
    const opt = [...sel.options].find((o) => o.value.includes("sofia.berg"));
    sel.value = opt.value;
    sel.dispatchEvent(new doc.defaultView.Event("change", { bubbles: true }));
    await new Promise((res) => setTimeout(res, 2500));
    // select that case in the sidebar, then let it re-render
    const btn = [...doc.querySelectorAll("#list .req")].find((b) => b.textContent.includes(c.number));
    check("new chat appears in the sidebar", !!btn);
    if (btn) {
      btn.dispatchEvent(new doc.defaultView.MouseEvent("click", { bubbles: true }));
      await new Promise((res) => setTimeout(res, 2500));
      const chips = doc.querySelectorAll(".chip");
      check("choice chips rendered", chips.length >= 2,
            `${chips.length} chips: ${[...chips].map((x) => x.textContent).join(" / ")}`);
      check("chips carry order ids", [...chips].some((x) => /ORD-\d+/.test(x.textContent)),
            [...chips].map((x) => x.textContent).join(" / "));
      check("status badge says needs reply",
            (doc.querySelector(".badge") || {}).textContent === "Needs your reply",
            (doc.querySelector(".badge") || {}).textContent);
    }
  }

  console.log("\n=== AGENT DESK (/) ===");
  {
    const { doc, errors } = await load("/", { waitMs: 4500 });
    check("no JS errors", errors.length === 0, errors.join(" | "));
    check("board columns rendered", doc.querySelectorAll(".column").length === 6,
          `${doc.querySelectorAll(".column").length} columns`);
    check("no duplicate top nav", doc.querySelectorAll(".topnav-links").length === 0);
    check("sidebar nav present", doc.querySelectorAll(".side-link").length === 4);
    check("agent roster rendered", doc.querySelectorAll("#team .member").length === 7);
    check("cards on the board", doc.querySelectorAll(".card").length > 0,
          `${doc.querySelectorAll(".card").length} cards`);
    check("channel label on cards", doc.querySelectorAll(".label.chan").length > 0);
    const cols = [...doc.querySelectorAll(".column-head")].map((h) => h.textContent.trim().split(" ")[0]);
    check("'With a human' column exists", cols.some((c) => c.startsWith("With")), cols.join(" | "));
    check("model pill resolved", !/^…$/.test(doc.querySelector("#model-pill").textContent));
  }

  console.log("\n=== AGENT DESK: case detail + triage audit ===");
  {
    const cases = await (await fetch(BASE + "/api/cases")).json();
    const waiting = cases.find((c) => c.status === "Waiting on Customer") || cases[0];
    const { doc, errors } = await load(`/#/case/${waiting.id}`, { waitMs: 5000 });
    check("no JS errors", errors.length === 0, errors.join(" | "));
    check("case title rendered", (doc.querySelector("#title") || {}).textContent?.length > 0);
    check("work log has entries", doc.querySelectorAll("#t-work > *").length > 0,
          `${doc.querySelectorAll("#t-work > *").length} entries`);
    check("pipeline rendered", doc.querySelectorAll(".pipeline .pl").length > 0);
    // switch to the conversation tab and look for the triage audit trail
    const tab = [...doc.querySelectorAll(".tab")].find((t) => t.dataset.t === "convo");
    if (tab) {
      tab.dispatchEvent(new doc.defaultView.MouseEvent("click", { bubbles: true }));
      await new Promise((res) => setTimeout(res, 1500));
      check("conversation rendered", doc.querySelectorAll("#convo .msg").length > 0);
      if (waiting.status === "Waiting on Customer") {
        check("triage question is labelled for the operator",
              doc.querySelectorAll(".offered").length > 0 || /triage Q/.test(doc.querySelector("#convo").innerHTML),
              doc.querySelector("#convo").innerHTML.slice(0, 200));
      }
    }
  }

  const failed = results.filter((r) => !r.ok);
  console.log(`\n${"=".repeat(50)}\n${results.length - failed.length}/${results.length} checks passed`);
  if (failed.length) {
    console.log("FAILED:");
    failed.forEach((f) => console.log(`  - ${f.name}: ${f.detail}`));
  }
  process.exit(failed.length ? 1 : 0);
})().catch((e) => { console.error("HARNESS CRASH:", e); process.exit(2); });
