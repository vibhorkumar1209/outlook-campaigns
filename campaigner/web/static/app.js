// Shared dashboard behaviour: tooltips, API helper, toasts, LinkedIn queue actions, run-now.
const $ = (s, el = document) => el.querySelector(s);
const $$ = (s, el = document) => [...el.querySelectorAll(s)];

async function api(path, body) {
  const r = await fetch(path, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body || {}),
  });
  const data = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(data.detail || (data.errors || []).join("; ") || r.statusText);
  return data;
}

function toast(msg, err) {
  const t = document.createElement("div");
  t.className = "toast" + (err ? " err" : "");
  t.textContent = msg;
  document.body.appendChild(t);
  setTimeout(() => t.remove(), err ? 6000 : 2500);
}

// Tooltips: any element with data-tip.
const tip = $("#tip");
document.addEventListener("mousemove", (e) => {
  const el = e.target.closest("[data-tip]");
  if (!el) { tip.hidden = true; return; }
  tip.textContent = el.dataset.tip;
  tip.hidden = false;
  const x = Math.min(e.clientX + 14, innerWidth - tip.offsetWidth - 8);
  tip.style.left = x + "px";
  tip.style.top = (e.clientY + 16) + "px";
});

// Confirm before destructive form posts.
document.addEventListener("submit", (e) => {
  const msg = e.target.dataset.confirm;
  if (msg && !confirm(msg)) e.preventDefault();
});

// Run now.
$$("[data-run]").forEach((b) => b.addEventListener("click", async () => {
  b.disabled = true;
  try { await api("/api/run"); toast("Run started. Refresh in a moment."); }
  catch (err) { toast(err.message, true); }
  setTimeout(() => (b.disabled = false), 3000);
}));

// LinkedIn queue.
function updateCount(card) {
  const ta = $(".task-body", card), c = $("[data-count]", card);
  if (!ta || !c) return;
  const n = ta.value.length;
  c.textContent = `${n}/300` + (n > 200 ? " · free LinkedIn accounts cap notes at 200" : "");
  c.classList.toggle("warn", n > 200);
}
$$(".task").forEach((card) => {
  updateCount(card);
  $(".task-body", card)?.addEventListener("input", () => updateCount(card));
  card.addEventListener("click", async (e) => {
    const btn = e.target.closest("[data-act]");
    if (!btn) return;
    const act = btn.dataset.act, ta = $(".task-body", card);
    if (act === "open") {
      if (ta && ta.value) {
        try { await navigator.clipboard.writeText(ta.value); toast("Copied. Paste it in LinkedIn."); }
        catch { ta.select(); document.execCommand("copy"); }
      }
      window.open(card.dataset.url, "_blank", "noopener");
      $("[data-act=done]", card).classList.add("primary");
      btn.classList.remove("primary");
      return;
    }
    $$("button", card).forEach((b) => (b.disabled = true));
    try {
      const r = await api(`/api/tasks/${card.dataset.task}`, { action: act, body: ta ? ta.value : null });
      card.classList.add("gone");
      setTimeout(() => card.remove(), 250);
      const badge = $("#nav-tasks .badge");
      if (badge) badge.textContent = r.open;
      const left = $("#tasks-left");
      if (left) left.textContent = r.open;
    } catch (err) {
      toast(err.message, true);
      $$("button", card).forEach((b) => (b.disabled = false));
    }
  });
});

// Lead page actions.
$$("[data-lead-act]").forEach((b) => b.addEventListener("click", async () => {
  const act = b.dataset.leadAct;
  if (b.dataset.ask && !confirm(b.dataset.ask)) return;
  const body = { action: act, channel: b.dataset.channel };
  if (act === "update") {
    body.email = $("#lead-email").value;
    body.linkedin_url = $("#lead-li").value;
  }
  try { await api(`/api/leads/${b.dataset.lead}`, body); location.reload(); }
  catch (err) { toast(err.message, true); }
}));

$$("[data-stop]").forEach((b) => b.addEventListener("click", async () => {
  if (!confirm("Stop all further steps for this lead in this campaign?")) return;
  try { await api(`/api/enrollments/${b.dataset.stop}/stop`); location.reload(); }
  catch (err) { toast(err.message, true); }
}));
