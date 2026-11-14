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
    if (act === "approve" || act === "likeonly") {
      $$("button", card).forEach((b) => (b.disabled = true));
      try {
        const r = act === "approve"
          ? await api(`/api/tasks/${card.dataset.task}/approve`, { comment: ta.value })
          : await api(`/api/tasks/${card.dataset.task}`, { action: "done", engage: { post_url: card.dataset.post, liked: true } });
        card.classList.add("gone");
        setTimeout(() => card.remove(), 250);
        const badge = $("#nav-tasks .badge");
        if (badge) badge.textContent = r.open;
        if (act === "approve") toast("Approved. The bot posts it on its next run.");
      } catch (err) {
        toast(err.message, true);
        $$("button", card).forEach((b) => (b.disabled = false));
      }
      return;
    }
    if (act === "posts") {
      window.open(btn.dataset.posts, "_blank", "noopener");
      return;
    }
    if (act === "copy") {
      if (!ta.value.trim()) return toast("No comment to copy yet", true);
      await navigator.clipboard.writeText(ta.value);
      $(".did-comment", card).checked = true;
      const url = $(".post-url", card).value.trim();
      if (/^https:\/\/(www\.)?linkedin\.com\//.test(url)) window.open(url, "_blank", "noopener");
      return toast("Copied. Paste it under the post.");
    }
    if (act === "draft") {
      btn.disabled = true;
      const label = btn.textContent;
      btn.textContent = "Drafting…";
      try {
        const r = await api(`/api/tasks/${card.dataset.task}/draft`, { post: $(".post-text", card).value });
        ta.value = r.comment;
        ta.focus();
      } catch (err) { toast(err.message, true); }
      btn.textContent = label;
      btn.disabled = false;
      return;
    }
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
      const engage = $(".did-like", card) ? {
        post_url: $(".post-url", card).value.trim(),
        liked: $(".did-like", card).checked,
        commented: $(".did-comment", card).checked,
      } : null;
      const r = await api(`/api/tasks/${card.dataset.task}`, { action: act, body: ta ? ta.value : null, engage });
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
  const body = { action: act, channel: b.dataset.channel, stage: b.dataset.stage };
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

$$("[data-resume]").forEach((b) => b.addEventListener("click", async () => {
  const ask = b.dataset.status === "bounced"
    ? "Resend the email that bounced and continue the sequence? Fix their email address first, or it will bounce again."
    : "Restart this lead's sequence from where it stopped?";
  if (!confirm(ask)) return;
  try { const r = await api(`/api/enrollments/${b.dataset.resume}/resume`); toast(`Resumed: ${r.how}`); setTimeout(() => location.reload(), 900); }
  catch (err) { toast(err.message, true); }
}));

// Rename a campaign in place (header or campaigns list). "dummy" in the name turns test mode on, so confirm that change.
$$("[data-rename]").forEach((b) => b.addEventListener("click", () => {
  const cid = b.dataset.rename, old = b.dataset.name;
  const target = document.getElementById("c-name")?.dataset.campaign === cid
    ? document.getElementById("c-name") : document.querySelector(`[data-name-of="${cid}"]`);
  const input = Object.assign(document.createElement("input"), { value: old, className: "rename-input",
    ariaLabel: "Campaign name", maxLength: 120 });
  target.replaceWith(input);
  b.hidden = true;
  input.focus();
  input.select();
  let done = false;
  const finish = async (save) => {
    if (done) return;
    done = true;
    const name = input.value.trim();
    if (!save || !name || name === old) { input.replaceWith(target); b.hidden = false; return; }
    const was = /dummy/i.test(old), now = /dummy/i.test(name);
    if (was !== now && !confirm(now
        ? "A name with “dummy” turns on test mode: the whole sequence runs within 30 minutes, ignoring the send window. Continue?"
        : "Removing “dummy” turns test mode off: the sequence goes back to its real timing. Continue?")) {
      input.replaceWith(target); b.hidden = false; return;
    }
    try {
      await api(`/api/campaigns/${cid}/rename`, { name });
      if (was !== now) return location.reload();  // test-mode badge and timings change
      target.textContent = name;
      b.dataset.name = name;
      if (target.id === "c-name") document.title = `${name} · Outreach`;
      toast("Renamed");
    } catch (err) { toast(err.message, true); }
    input.replaceWith(target);
    b.hidden = false;
  };
  input.addEventListener("keydown", (e) => {
    if (e.key === "Enter") { e.preventDefault(); finish(true); }
    if (e.key === "Escape") finish(false);
  });
  input.addEventListener("blur", () => finish(true));
}));

// Campaign Status: Lead Status saves as you type.
$$("[data-tracker]").forEach((inp) => {
  let timer;
  inp.addEventListener("input", () => {
    clearTimeout(timer);
    timer = setTimeout(async () => {
      const eid = inp.dataset.tracker;
      const body = { lead_status: inp.value };
      try { await api(`/api/enrollments/${eid}/tracker`, body); inp.classList.remove("err"); }
      catch (err) { inp.classList.add("err"); toast(err.message, true); }
    }, 500);
  });
});

// ---------- CRM ----------

// Owner: Campaign Status cells and the lead page's "Assigned to".
$$("[data-owner-of]").forEach((sel) => sel.addEventListener("change", async () => {
  try {
    await api(`/api/leads/${sel.dataset.ownerOf}`, { action: "owner", owner_id: sel.value });
    // The same person can appear in several campaigns' rows: keep them in step.
    $$(`[data-owner-of="${sel.dataset.ownerOf}"]`).forEach((o) => { o.value = sel.value; });
    $$(`[data-log][data-lead="${sel.dataset.ownerOf}"]`).forEach((b) => { b.dataset.owner = sel.value; });
    toast("Owner saved");
  } catch (err) { toast(err.message, true); }
}));

// Campaign Status: History expands every communication with the person under their row.
$$("[data-history]").forEach((b) => b.addEventListener("click", async () => {
  const row = b.closest("tr").nextElementSibling;
  const open = row.hidden;
  row.hidden = !open;
  b.setAttribute("aria-expanded", String(open));
  b.textContent = open ? "Hide" : "History";
  if (open) {
    try {
      const r = await fetch(`/leads/${b.dataset.history}/history`);
      $(".history-box", row).innerHTML = await r.text();
    } catch (err) { $(".history-box", row).textContent = "Couldn't load: " + err.message; }
  }
}));

// Log a call / meeting / note.
const dlg = $("#log-dlg");
if (dlg) {
  const form = $("#log-form");
  const pad = (n) => String(n).padStart(2, "0");
  const nowLocal = () => {
    const d = new Date();
    return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}T${pad(d.getHours())}:${pad(d.getMinutes())}`;
  };
  const responded = new Set(JSON.parse(dlg.dataset.responded || "[]"));
  const syncOutcome = () => {
    form.stop_sequences.checked = form.kind.value !== "note" && responded.has(form.outcome.value);
  };
  const syncKind = () => {
    const kind = form.kind.value;
    $("#log-title").textContent = { call: "Log a call", meeting: "Log a meeting", note: "Add a note" }[kind];
    $$("[data-for]", form).forEach((el) => { el.hidden = !el.dataset.for.split(" ").includes(kind); });
    $$("option[data-kind]", form.outcome).forEach((o) => {
      o.hidden = o.disabled = !o.dataset.kind.split(" ").includes(kind);
    });
    const cur = form.outcome.selectedOptions[0];
    if (!cur || cur.hidden) form.outcome.value = "";  // always a deliberate choice, never a default outcome
    syncOutcome();
  };
  $$("input[name=kind]", form).forEach((r) => r.addEventListener("change", syncKind));
  form.outcome.addEventListener("change", syncOutcome);

  document.addEventListener("click", (e) => {
    const b = e.target.closest("[data-log]");
    if (!b) return;
    form.reset();
    form.lead.value = b.dataset.lead;
    form.campaign_id.value = b.dataset.campaign || "";
    form.at.value = nowLocal();
    form.by_id.value = dlg.dataset.me || b.dataset.owner || "";
    $("#log-who").textContent = [b.dataset.name, b.dataset.company].filter(Boolean).join(" · ");
    const phone = b.dataset.phone || "";
    const ph = $("#log-phone");
    ph.textContent = "";
    if (phone) {
      const a = document.createElement("a");
      a.href = "tel:" + phone.replace(/\s+/g, "");
      a.textContent = "☎ " + phone;
      ph.appendChild(a);
    }
    syncKind();
    dlg.showModal();
  });

  form.addEventListener("submit", async (e) => {
    if (e.submitter && e.submitter.value === "cancel") return;
    e.preventDefault();
    const kind = form.kind.value;
    if (kind !== "note" && !form.outcome.value) { toast("Pick an outcome", true); form.outcome.focus(); return; }
    const body = {
      kind, at: form.at.value, outcome: kind === "note" ? "" : form.outcome.value,
      duration_min: kind === "note" ? "" : form.duration_min.value, notes: form.notes.value,
      next_step: form.next_step.value, next_step_at: form.next_step_at.value, by_id: form.by_id.value,
      campaign_id: form.campaign_id.value, stop_sequences: kind !== "note" && form.stop_sequences.checked,
    };
    $("#log-save").disabled = true;
    try {
      await api(`/api/leads/${form.lead.value}/activities`, body);
      dlg.close();
      toast("Logged");
      setTimeout(() => location.reload(), 400);
    } catch (err) { toast(err.message, true); }
    finally { $("#log-save").disabled = false; }
  });
}

document.addEventListener("click", async (e) => {
  const b = e.target.closest("[data-del-activity]");
  if (!b || !confirm("Delete this entry? This can't be undone.")) return;
  try { await api(`/api/activities/${b.dataset.delActivity}/delete`); location.reload(); }
  catch (err) { toast(err.message, true); }
});
