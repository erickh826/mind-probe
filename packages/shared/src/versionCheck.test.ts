import assert from "node:assert/strict";
import test from "node:test";
import {
  buildIdFrom,
  createVersionMonitor,
  readBuildId,
  VERSION_CHECK_MIN_GAP_MS,
  versionJsonPlugin,
} from "./versionCheck.ts";

function json(body: unknown, status = 200) {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "content-type": "application/json" },
  });
}

function harness(responses: Array<Response | Error>) {
  const calls: Array<{ url: string; init: RequestInit }> = [];
  const ticks: Array<() => void> = [];
  let cleared = 0;
  let clock = 0;
  const visibility = {
    visibilityState: "visible",
    listeners: new Set<() => void>(),
    addEventListener(_type: "visibilitychange", listener: () => void) {
      this.listeners.add(listener);
    },
    removeEventListener(_type: "visibilitychange", listener: () => void) {
      this.listeners.delete(listener);
    },
  };
  const monitor = createVersionMonitor({
    currentBuildId: "build-a",
    fetchImpl: async (url, init) => {
      calls.push({ url, init });
      const next = responses.shift() ?? json({ buildId: "build-a" });
      if (next instanceof Error) {
        throw next;
      }
      return next;
    },
    now: () => clock,
    setIntervalImpl: (callback) => {
      ticks.push(callback);
      return ticks.length;
    },
    clearIntervalImpl: () => {
      cleared += 1;
    },
    visibility,
  });
  return {
    monitor,
    calls,
    visibility,
    tick: () => ticks.at(-1)?.(),
    advance: (ms: number) => {
      clock += ms;
    },
    get cleared() {
      return cleared;
    },
  };
}

const settle = () => new Promise((resolve) => setTimeout(resolve, 0));

test("buildIdFrom uses the Vercel commit, else a timestamp", () => {
  assert.equal(buildIdFrom({ VERCEL_GIT_COMMIT_SHA: " abc123 " }), "abc123");
  assert.equal(buildIdFrom({}, 36), "local-10");
  assert.equal(buildIdFrom({ VERCEL_GIT_COMMIT_SHA: "" }, 35), "local-z");
});

test("the plugin emits version.json with the same id", () => {
  const emitted: Array<{ fileName: string; source: string }> = [];
  const plugin = versionJsonPlugin("abc123");
  assert.equal(plugin.apply, "build");
  plugin.generateBundle.call({
    emitFile(file) {
      emitted.push(file);
      return "ref";
    },
  });
  assert.equal(emitted.length, 1);
  assert.equal(emitted[0].fileName, "version.json");
  assert.deepEqual(JSON.parse(emitted[0].source), { buildId: "abc123" });
});

test("readBuildId accepts only a JSON object with a short string id", async () => {
  assert.equal(await readBuildId(json({ buildId: "b" })), "b");
  // An old deploy without the file: the SPA rewrite serves index.html with 200.
  const html = new Response("<!doctype html><html></html>", {
    status: 200,
    headers: { "content-type": "text/html; charset=utf-8" },
  });
  assert.equal(await readBuildId(html), null);
  assert.equal(await readBuildId(json({ buildId: "b" }, 404)), null);
  assert.equal(await readBuildId(json({ buildId: "" })), null);
  assert.equal(await readBuildId(json({ buildId: 7 })), null);
  assert.equal(await readBuildId(json({ buildId: "x".repeat(201) })), null);
  assert.equal(await readBuildId(json(["b"])), null);
  assert.equal(await readBuildId(json(null)), null);
  const broken = new Response("{not json", { headers: { "content-type": "application/json" } });
  assert.equal(await readBuildId(broken), null);
});

test("same build stays quiet; a different build is reported once and polling stops", async () => {
  const h = harness([json({ buildId: "build-a" }), json({ buildId: "build-b" })]);
  let notified = 0;
  const unsubscribe = h.monitor.subscribe(() => {
    notified += 1;
  });
  await settle();
  assert.equal(h.calls.length, 1, "checks once on subscribe");
  assert.equal(h.calls[0].url, "/version.json");
  assert.equal(h.calls[0].init.cache, "no-store");
  assert.equal(h.monitor.getSnapshot(), null);
  assert.equal(notified, 0);

  h.tick();
  await settle();
  assert.equal(h.monitor.getSnapshot(), "build-b");
  assert.equal(notified, 1);
  assert.equal(h.cleared, 1, "the interval stops after the first hit");
  assert.equal(h.visibility.listeners.size, 0);

  await h.monitor.checkNow();
  assert.equal(h.calls.length, 2, "no more requests once outdated");
  unsubscribe();
});

test("network errors and old deploys never report an update", async () => {
  const html = new Response("<html>", { headers: { "content-type": "text/html" } });
  const h = harness([new Error("offline"), html, json({}, 500)]);
  h.monitor.subscribe(() => assert.fail("must not notify"));
  await settle();
  h.tick();
  await settle();
  h.tick();
  await settle();
  assert.equal(h.calls.length, 3);
  assert.equal(h.monitor.getSnapshot(), null);
});

test("becoming visible checks, but not more often than the minimum gap", async () => {
  const h = harness([]);
  const unsubscribe = h.monitor.subscribe(() => {});
  await settle();
  assert.equal(h.calls.length, 1);
  for (const listener of h.visibility.listeners) listener();
  await settle();
  assert.equal(h.calls.length, 1, "too soon after the last check");
  h.advance(VERSION_CHECK_MIN_GAP_MS);
  h.visibility.visibilityState = "hidden";
  for (const listener of h.visibility.listeners) listener();
  await settle();
  assert.equal(h.calls.length, 1, "hidden tabs do not check");
  h.visibility.visibilityState = "visible";
  for (const listener of h.visibility.listeners) listener();
  await settle();
  assert.equal(h.calls.length, 2);

  unsubscribe();
  assert.equal(h.visibility.listeners.size, 0, "last unsubscribe stops listening");
});

test("a disabled monitor never fetches", async () => {
  let fetched = 0;
  const monitor = createVersionMonitor({
    currentBuildId: "dev",
    enabled: false,
    fetchImpl: async () => {
      fetched += 1;
      return json({ buildId: "other" });
    },
    visibility: null,
  });
  monitor.subscribe(() => {});
  await monitor.checkNow();
  assert.equal(fetched, 0);
  assert.equal(monitor.getSnapshot(), null);
});
