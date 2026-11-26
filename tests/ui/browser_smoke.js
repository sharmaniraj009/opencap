// Browser smoke test of the web UI (run by tests/integration/test_ui_browser.py when Node and
// Playwright are installed; skipped otherwise). Drives a real Chromium against a running server:
//   node tests/ui/browser_smoke.js <port> <admin-token> <viewer-token> <project.json> [screenshot-dir]
// Fails on any assertion, any console error or any Content-Security-Policy violation.
"use strict";
const { chromium } = require("playwright");

const [port, adminToken, viewerToken, projectFile, shots] = process.argv.slice(2);
const base = `http://127.0.0.1:${port}`;
const DEV = "SITE MODBUS/Device1";
const devHash = "#/device/SITE%20MODBUS/Device1";
const editHash = "#/edit/SITE%20MODBUS/Device1";
const problems = [];

function check(cond, what) {
  if (!cond) throw new Error("check failed: " + what);
  console.log("ok   " + what);
}

function watch(page, label) {
  page.on("console", (m) => {
    // expected 4xx answers (wrong token, validation errors) log network lines; script errors and
    // CSP violations ("Refused to ...") still fail the run
    if ((m.type() === "error" || m.type() === "warning") && !/status of 4\d\d/.test(m.text())) problems.push(`${label} console ${m.type()}: ${m.text()}`);
  });
  page.on("pageerror", (e) => problems.push(`${label} pageerror: ${e.message}`));
}

async function shot(page, name) {
  if (shots) await page.screenshot({ path: `${shots}/${name}.png`, fullPage: true });
}

// ---------------------------------------------------------------- language tables (#126)
// A test-only pseudo language "de" wraps every string of i18n/en.json in ⟦ ⟧. A browser that
// prefers de-DE gets it; any visible text or attribute outside the markers that is not data from
// the API (device names, values, server messages) is a string that does not come from the table.
const OPEN = "⟦", CLOSE = "⟧";
const LANGUAGES = { en: "English", de: "Pseudo" };

async function withPseudoTables(context, en) {
  const pseudo = {};
  for (const [key, text] of Object.entries(en)) pseudo[key] = OPEN + text + CLOSE;
  const json = (body) => (route) => route.fulfill({ contentType: "application/json", body: JSON.stringify(body) });
  await context.route("**/ui/i18n/languages.json", json(LANGUAGES));
  await context.route("**/ui/i18n/de.json", json(pseudo));
}

// every string (and object key) the page receives from the API or the live feed; API answers are
// read on the way to the page (a response listener can miss a body evicted from the browser cache)
async function collectData(page) {
  const data = new Set(["opencap", ...Object.values(LANGUAGES)]);
  const add = (v) => {
    if (typeof v === "string") { data.add(v); v.split("/").forEach((p) => data.add(p)); }
    else if (typeof v === "boolean") data.add(String(v));
    else if (Array.isArray(v)) v.forEach(add);
    else if (v && typeof v === "object") for (const [k, x] of Object.entries(v)) { add(k); add(x); }
  };
  await page.route("**/api/v1/**", async (route) => {
    const resp = await route.fetch();
    try { add(JSON.parse(await resp.text())); } catch (e) { /* not JSON */ }
    await route.fulfill({ response: resp });
  });
  page.on("websocket", (ws) => ws.on("framereceived", (f) => { try { add(JSON.parse(f.payload)); } catch (e) { /* not JSON */ } }));
  return data;
}

// visible text and attributes outside the markers, minus data; [] when everything is translated
async function untranslated(page, data) {
  const found = await page.evaluate(([open, close]) => {
    const out = [];
    const shown = (el) => el.checkVisibility({ visibilityProperty: true });
    const outside = (text, depth) => {            // the characters of text at marker depth 0
      let rest = "";
      for (const ch of text) {
        if (ch === open) depth.n += 1;
        else if (ch === close) depth.n = Math.max(0, depth.n - 1);
        else if (depth.n === 0) rest += ch;
      }
      return rest;
    };
    const depth = { n: 0 };
    const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
    for (let node = walker.nextNode(); node; node = walker.nextNode()) {
      const el = node.parentElement;
      if (!el || el.closest("script, style, noscript, template, option") || !shown(el)) continue;
      const rest = outside(node.data, depth);
      if (/[A-Za-z]{2}/.test(rest)) out.push({ text: rest.trim(), where: el.outerHTML.slice(0, 160) });
    }
    for (const el of document.body.querySelectorAll("*")) {
      const select = el.localName === "option" ? el.closest("select") : null;
      if (!(select ? shown(select) : shown(el))) continue;
      const texts = ["placeholder", "title", "aria-label", "data-label", "alt"].map((a) => [a, el.getAttribute(a)]);
      if (select) texts.push(["option", el.textContent]);
      for (const [attr, value] of texts) {
        if (!value) continue;
        const rest = outside(value, { n: 0 });
        if (/[A-Za-z]{2}/.test(rest)) out.push({ text: rest.trim(), where: attr + " of " + el.outerHTML.slice(0, 160) });
      }
    }
    return out;
  }, [OPEN, CLOSE]);
  const known = [...data].filter((s) => /[A-Za-z]{2}/.test(s)).sort((a, b) => b.length - a.length);
  return found.filter((f) => {
    let rest = f.text;
    for (const s of known) if (rest.includes(s)) rest = rest.split(s).join(" ");
    rest = rest.replace(/[A-Za-z_]+…/g, " ");      // the cut-off end of a long value (short())
    return /[A-Za-z]{2}/.test(rest);
  });
}

async function allTranslated(page, data, what) {
  const left = await untranslated(page, data);
  if (left.length) console.log(left.map((f) => `  "${f.text}" in ${f.where}`).join("\n"));
  check(left.length === 0, "every visible string comes from the table: " + what);
}

async function pseudoLocale(browser, en) {
  const ctx = await browser.newContext({ viewport: { width: 1280, height: 900 }, locale: "de-DE" });
  await withPseudoTables(ctx, en);
  const page = await ctx.newPage();
  watch(page, "pseudo");
  const data = await collectData(page);

  await page.goto(base + "/ui/");
  await page.waitForSelector("#view-login");
  check(await page.evaluate(() => document.documentElement.lang) === "de", "a de-DE browser gets the de table");
  await allTranslated(page, data, "login");
  await page.fill("#view-login input[name=token]", adminToken);
  await page.click("#view-login button[type=submit]");
  await page.waitForSelector(`#view-dashboard a[href="${devHash}"]`);
  await allTranslated(page, data, "dashboard");

  await page.goto(base + "/ui/" + devHash);
  await page.waitForSelector("#view-device #identity table:visible");
  await page.waitForSelector("#view-device details summary");
  await page.click("#view-device .actions button.primary");          // read once
  await page.waitForSelector("#view-device .flash-ok:visible");
  await allTranslated(page, data, "device page");

  await page.goto(base + "/ui/#/quick/" + encodeURIComponent("SITE MODBUS") + "/Device1");
  await page.waitForSelector("#view-quick #quick-mode.mode-live");
  await page.waitForSelector('#quick-values tr[data-tag="FLOW_1"]');
  await allTranslated(page, data, "Quick Client");

  await page.goto(base + "/ui/#/config");
  await page.waitForSelector("#view-config tbody tr");
  await allTranslated(page, data, "config");
  await page.goto(base + "/ui/#/channel/SITE%20MODBUS");
  await page.waitForSelector("#view-channel tbody tr");
  await allTranslated(page, data, "channel");

  await page.goto(base + "/ui/" + editHash);
  await page.waitForSelector("#tags-body tr");
  await page.locator("#tags-body tr").first().locator("button.small:not(.danger)").click();   // inline tag editor
  await page.waitForSelector("#tags-body tr.editing input[name=address]");
  await allTranslated(page, data, "device editor");
  await page.locator("#tags-body tr.editing button:not(.primary)").click();
  await page.fill("#device-form input[name=timeout_ms]", "1600");
  await page.click("#device-form button[type=submit]");
  await page.waitForSelector("#device-form .flash-ok:visible");
  await page.waitForSelector("#pending:visible");
  await allTranslated(page, data, "saved note and pending-reload banner");
  await page.click("#pending button");
  await page.waitForSelector("#flash .flash-ok:visible");
  await allTranslated(page, data, "reload flash");

  await page.goto(base + "/ui/#/check");
  await page.waitForSelector("#view-check .cards");
  await allTranslated(page, data, "check");
  await page.goto(base + "/ui/#/import");
  await page.setInputFiles("#import-form input[type=file]", projectFile);
  await page.click("#import-form button[type=submit]");
  await page.waitForSelector("#import-result h2");
  await allTranslated(page, data, "import dry run");
  await page.goto(base + "/ui/#/audit");
  await page.waitForSelector("#view-audit tbody tr");
  await allTranslated(page, data, "audit");
  await shot(page, "pseudo_locale_audit");

  // a choice in the header is kept for the tab, not for a new one
  await page.selectOption("select[name=lang]", "en");
  await page.waitForSelector("#view-audit h1 >> text=Audit log");
  check(await page.evaluate(() => sessionStorage.getItem("opencap.lang")) === "en", "the language choice is kept in sessionStorage");
  await page.reload();
  await page.waitForSelector("#view-audit h1 >> text=Audit log");
  check(await page.evaluate(() => document.documentElement.lang) === "en", "the choice survives a reload of the tab");
  const tab = await ctx.newPage();
  watch(tab, "pseudo tab");
  await tab.goto(base + "/ui/");
  await tab.waitForSelector("#view-login h1");
  check((await tab.textContent("#view-login h1")) === OPEN + en["login.log_in"] + CLOSE, "a new tab follows the browser again");

  // a preferred language without a table falls back to English
  const fr = await browser.newContext({ locale: "fr-FR" });
  await withPseudoTables(fr, en);
  const frPage = await fr.newPage();
  watch(frPage, "fr");
  await frPage.goto(base + "/ui/");
  await frPage.waitForSelector("#view-login h1");
  check((await frPage.textContent("#view-login h1")) === en["login.log_in"], "fr-FR, which has no table, gets English");
  await fr.close();
  await ctx.close();
}

(async () => {
  const browser = await chromium.launch();
  try {
    const page = await (await browser.newContext({ viewport: { width: 1280, height: 900 } })).newPage();
    watch(page, "desktop");

    // --- login
    await page.goto(base + "/ui/");
    await page.waitForSelector("#view-login");
    await page.fill("#view-login input[name=token]", "wrong-token");
    await page.click("#view-login button[type=submit]");
    await page.waitForSelector("#view-login .error >> text=Unknown token");
    check(true, "a wrong token is refused");
    await page.fill("#view-login input[name=token]", adminToken);
    await page.click("#view-login button[type=submit]");
    await page.waitForSelector("#view-dashboard .card >> text=MQTT link");
    check(await page.evaluate(() => sessionStorage.getItem("opencap.token") !== null), "token kept in sessionStorage");
    check(await page.locator('footer.foot a[href="vendor/alpine-LICENSE.md"]').textContent() === "Alpine.js licence", "text with markup keeps its link");

    // --- dashboard
    await page.waitForSelector("#view-dashboard >> text=SITE M800");
    check((await page.textContent("#view-dashboard .cards")).includes("connected"), "dashboard shows the MQTT link");
    check(await page.locator("#view-dashboard .state-demoted").count() === 1, "dashboard shows a demoted device");
    await page.fill("#view-dashboard input[type=search]", "m800");
    check(await page.locator("#view-dashboard tbody tr").count() === 1, "dashboard filter");
    await page.fill("#view-dashboard input[type=search]", "");
    await shot(page, "dashboard");

    // --- device page
    await page.click(`#view-dashboard a[href="${devHash}"]`);
    await page.waitForSelector("#view-device >> text=FLOW_1");
    await page.waitForSelector("#view-device #identity >> text=Example Vendor");
    check(true, "device page shows values and identity");
    check((await page.textContent("#view-device")).includes("Block plan"), "device page shows the block plan");
    await page.click("#view-device >> text=Read once");
    await page.waitForSelector("#view-device >> text=Read once at");
    check(true, "read once");
    await shot(page, "device");

    // --- Quick Client: live values over the WebSocket, demand read
    await page.click("#view-device >> text=Quick Client");
    await page.waitForSelector("#view-quick #quick-mode >> text=live");
    await page.waitForSelector('#quick-values tr[data-tag="FLOW_1"]');
    check((await page.textContent('#view-quick nav a[aria-current="true"]')).includes("Device1"), "quick client selects the device in the tree");
    const flow = async () => page.textContent('#quick-values tr[data-tag="FLOW_1"] td:nth-child(4)');
    const first = await flow();
    await page.waitForFunction((v) => {
      const td = document.querySelector('#quick-values tr[data-tag="FLOW_1"] td:nth-child(4)');
      return td && td.textContent !== v;
    }, first, { timeout: 5000 });
    check(true, "quick client value changes without a reload (" + first + " → " + (await flow()) + ")");
    check(/\d+ s/.test(await page.textContent('#quick-values tr[data-tag="FLOW_1"] td:nth-child(7)')), "quick client shows the value's age");
    check(Number(await page.textContent('#quick-values tr[data-tag="FLOW_1"] td:nth-child(8)')) >= 1, "quick client counts updates");
    check((await page.textContent('#quick-values tr[data-tag="FLOW_1"] td:nth-child(2)')).trim() !== "", "quick client shows the address from the config");
    await page.fill("#view-quick input[aria-label='Filter tags']", "flow_1");
    check(await page.locator("#quick-values tbody tr").count() === 1, "quick client tag filter");
    await page.fill("#view-quick input[aria-label='Filter tags']", "");
    await page.click("#view-quick >> text=Read now");
    await page.waitForSelector("#view-quick >> text=Read now at");
    check(true, "quick client demand read");
    await page.goto(base + "/ui/#/quick/NOPE/X");
    await page.waitForSelector("#view-quick .banner >> text=Not polled");
    check(true, "quick client reports a device that is not polled");
    await page.goto(base + "/ui/#/quick/" + encodeURIComponent("SITE MODBUS") + "/Device1");
    await page.waitForSelector('#quick-values tr[data-tag="FLOW_1"]');
    await shot(page, "quick_client");

    // --- device editor: inline validation error, then a good save
    await page.goto(base + "/ui/" + editHash);
    await page.waitForSelector("#device-form input[name=timeout_ms]");
    await page.fill("#device-form input[name=timeout_ms]", "soon");
    await page.click("#device-form button[type=submit]");
    await page.waitForSelector("#device-form .field-error:visible");
    check((await page.locator("#device-form .field-error:visible").first().textContent()).includes("integer"), "inline field error");
    await page.fill("#device-form input[name=timeout_ms]", "1500");
    await page.click("#device-form button[type=submit]");
    await page.waitForSelector("#device-form .flash-ok:visible");
    await page.waitForSelector("#pending:visible");
    check((await page.textContent("#pending")).includes("SITE MODBUS"), "pending-reload banner names the channel");

    // inline tag edit: a bad address is refused next to the field, a good one saves
    await page.locator("#tags-body tr").first().locator("button:has-text(\"Edit\")").click();
    await page.fill("#tags-body tr.editing input[name=address]", "999999");
    await page.click("#tags-body tr.editing button.primary");
    await page.waitForSelector("#tags-body tr.editing .field-error:visible");
    check((await page.textContent("#tags-body tr.editing")).includes("unknown area prefix"), "tag address error inline");
    await page.fill("#tags-body tr.editing input[name=address]", "400201");
    await page.click("#tags-body tr.editing button.primary");
    await page.waitForSelector("#tags-body .saved:visible");
    // add a tag
    await page.fill("#tag-create input[name=name]", "NEW_TAG");
    await page.fill("#tag-create input[name=address]", "400301");
    await page.click("#tag-create button[type=submit]");
    await page.waitForSelector("#tags-body >> text=NEW_TAG");
    check(await page.inputValue("#tag-create input[name=name]") === "", "add tag clears the form");
    // publish map and override
    await page.fill("#map-form input[name=sub_device_id]", "SUB_CONFIRMED");
    await page.uncheck("#map-form input[name=stub]");
    await page.click("#map-form button[type=submit]");
    await page.waitForSelector("#map-form .flash-ok:visible");
    await page.fill("#override-form textarea", "{\"timeout_ms\": [");
    await page.click("#override-form button[type=submit]");
    await page.waitForSelector("#override-form .error-box:visible");
    await page.fill("#override-form textarea", "{\"timeout_ms\": 2500, \"tags\": {\"NEW_TAG\": {\"enabled\": false}}}");
    await page.click("#override-form button[type=submit]");
    await page.waitForSelector("#override-form .flash-ok:visible");
    check(true, "map entry and override saved");
    await shot(page, "editor");

    // reload from the banner
    await page.click("#pending >> text=Reload now");
    await page.waitForSelector("#flash .flash-ok >> text=Reloaded");
    check((await page.textContent("#flash")).includes("restarted SITE MODBUS"), "reload restarts the changed channel");

    // --- config overview and channel page
    await page.goto(base + "/ui/#/config");
    await page.waitForSelector("#view-config >> text=SITE PCCC");
    await page.goto(base + "/ui/#/channel/SITE%20PCCC");
    await page.waitForSelector("#view-channel >> text=7 PUMP");
    page.once("dialog", (d) => d.accept());
    await page.click("#view-channel >> text=Delete channel");
    await page.waitForSelector("#view-config");
    check(!(await page.textContent("#view-config")).includes("SITE PCCC"), "channel deleted");

    // --- import: dry run, then apply
    await page.goto(base + "/ui/#/import");
    await page.setInputFiles("#import-form input[type=file]", projectFile);
    await page.click("#import-form button[type=submit]");
    await page.waitForSelector("#import-result h2 >> text=Dry run");
    check((await page.textContent("#import-result")).includes("SITE PCCC/7 PUMP"), "import dry run lists the added device");
    await shot(page, "import");
    page.once("dialog", (d) => d.accept());
    await page.click("#import-result >> text=Apply import");
    await page.waitForSelector("#import-result >> text=Imported");
    check(true, "import applied");

    // --- audit and check
    await page.goto(base + "/ui/#/audit");
    await page.waitForSelector("#view-audit >> text=config.import_kep");
    check(await page.locator("#view-audit tbody tr").count() >= 8, "audit lists the changes");
    await shot(page, "audit");
    await page.goto(base + "/ui/#/check");
    await page.waitForSelector("#view-check >> text=Requests / full pass");
    await shot(page, "check");

    // --- phone width, viewer token: read-only and no horizontal scrolling
    const phone = await (await browser.newContext({ viewport: { width: 375, height: 800 }, isMobile: true, hasTouch: true })).newPage();
    watch(phone, "phone");
    await phone.goto(base + "/ui/");
    await phone.fill("#view-login input[name=token]", viewerToken);
    await phone.click("#view-login button[type=submit]");
    await phone.waitForSelector("#view-dashboard .card >> text=MQTT link");
    const width = async () => phone.evaluate(() => document.documentElement.scrollWidth);
    check(await width() <= 375, "phone dashboard fits the width");
    await shot(phone, "phone_dashboard");
    await phone.goto(base + "/ui/" + devHash);
    await phone.waitForSelector("#view-device >> text=FLOW_1");
    check(!(await phone.isVisible("#view-device >> text=Read once")), "viewer has no read once");
    check(await width() <= 375, "phone device page fits the width");
    await phone.goto(base + "/ui/#/quick/SITE%20MODBUS/Device1");
    await phone.waitForSelector("#view-quick #quick-mode >> text=live");
    await phone.waitForSelector('#quick-values tr[data-tag="FLOW_1"]');
    check(!(await phone.isVisible("#view-quick >> text=Read now")), "viewer has no read now");
    check(await width() <= 375, "phone quick client fits the width");
    await shot(phone, "phone_quick_client");
    await phone.goto(base + "/ui/" + editHash);
    await phone.waitForSelector("#device-form input[name=name]");
    check(await phone.isDisabled("#device-form input[name=name]"), "viewer sees the editor read-only");
    check(!(await phone.isVisible("#tag-create")), "viewer cannot add tags");
    await shot(phone, "phone_editor");
    await phone.click("text=Log out");
    await phone.waitForSelector("#view-login");
    check(await phone.evaluate(() => sessionStorage.getItem("opencap.token") === null), "log out forgets the token");
    check(await phone.evaluate(() => document.documentElement.lang) === "en", "an en-US browser gets English");

    await pseudoLocale(browser, await page.evaluate(() => fetch("i18n/en.json").then((r) => r.json())));
  } finally {
    await browser.close();
  }
  if (problems.length) {
    console.log("PROBLEMS:\n" + problems.join("\n"));
    process.exit(1);
  }
  console.log("browser smoke: all checks passed");
})().catch((e) => { console.error("FAILED", e.message); if (problems.length) console.error(problems.join("\n")); process.exit(1); });
