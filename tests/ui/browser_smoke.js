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
    await phone.goto(base + "/ui/" + editHash);
    await phone.waitForSelector("#device-form input[name=name]");
    check(await phone.isDisabled("#device-form input[name=name]"), "viewer sees the editor read-only");
    check(!(await phone.isVisible("#tag-create")), "viewer cannot add tags");
    await shot(phone, "phone_editor");
    await phone.click("text=Log out");
    await phone.waitForSelector("#view-login");
    check(await phone.evaluate(() => sessionStorage.getItem("opencap.token") === null), "log out forgets the token");
  } finally {
    await browser.close();
  }
  if (problems.length) {
    console.log("PROBLEMS:\n" + problems.join("\n"));
    process.exit(1);
  }
  console.log("browser smoke: all checks passed");
})().catch((e) => { console.error("FAILED", e.message); if (problems.length) console.error(problems.join("\n")); process.exit(1); });
