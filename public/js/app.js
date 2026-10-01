const out = document.getElementById("output");

async function show(url) {
  const t0 = performance.now();
  const res = await fetch(url);
  const body = await res.text();
  let pretty = body;
  try { pretty = JSON.stringify(JSON.parse(body), null, 2); } catch {}
  out.textContent = `${res.status} ${res.statusText}  (${(performance.now() - t0).toFixed(0)} ms)\n\n${pretty}`;
}

document.querySelectorAll("button[data-url]").forEach((b) =>
  b.addEventListener("click", () => show(b.dataset.url)));

document.getElementById("parallel").addEventListener("click", async () => {
  out.textContent = "Running 5 requests that each sleep 1s…";
  const t0 = performance.now();
  // Unique URLs: browsers serialize concurrent GETs to the *same* URL (HTTP cache lock).
  const results = await Promise.all(
    Array.from({ length: 5 }, (_, i) =>
      fetch(`/api/slow?seconds=1&n=${i}`, { cache: "no-store" }).then((r) => r.json())));
  const ms = (performance.now() - t0).toFixed(0);
  out.textContent = `5 × 1s requests finished in ${ms} ms total (thread pool at work)\n\n` +
    results.map((r) => `slept ${r.slept}s on ${r.worker}`).join("\n");
});
