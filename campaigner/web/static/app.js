// Shared dashboard behaviour: tooltips, API helper, toasts, task queue, drafts, CRM dialogs, run-now.
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

// Task queue (LinkedIn and WhatsApp).
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
    if (act === "open" && card.dataset.wa) {
      window.open(`https://wa.me/${card.dataset.wa}?text=${encodeURIComponent(ta ? ta.value : "")}`, "_blank", "noopener");
      $("[data-act=done]", card).classList.add("primary");
      btn.classList.remove("primary");
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
    if ($("#lead-phone")) body.phone = $("#lead-phone").value;
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

// Log a call / meeting / message / note. The lead's status is confirmed (or changed) with every log.
const dlg = $("#log-dlg");
if (dlg) {
  const form = $("#log-form");
  const pad = (n) => String(n).padStart(2, "0");
  const nowLocal = () => {
    const d = new Date();
    return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}T${pad(d.getHours())}:${pad(d.getMinutes())}`;
  };
  const responded = new Set(JSON.parse(dlg.dataset.responded || "[]"));
  const suggested = JSON.parse(dlg.dataset.suggested || "{}");
  const TITLES = { call: "Log a call", meeting: "Log a meeting", whatsapp: "Log a WhatsApp / SMS", linkedin: "Log a LinkedIn message", note: "Add a note" };
  let current = "New";
  const syncOutcome = () => {
    const out = form.kind.value === "note" ? "" : form.outcome.value;
    form.stop_sequences.checked = responded.has(out);
    const want = suggested[out];
    form.status.value = want && !["Converted", "Closed/Lost"].includes(current) ? want : current;
    $("#log-status-hint").textContent = form.status.value !== current
      ? `Was ${current}. Suggested from the outcome; change it if that's wrong.` : "Confirm it, or change it.";
  };
  const syncKind = () => {
    const kind = form.kind.value;
    $("#log-title").textContent = TITLES[kind];
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

  window.openLog = (b, kind) => {
    form.reset();
    form.lead.value = b.dataset.lead;
    form.campaign_id.value = b.dataset.campaign || "";
    form.at.value = nowLocal();
    form.by_id.value = dlg.dataset.me || b.dataset.owner || "";
    current = b.dataset.status || "New";
    if (kind) form.kind.value = kind;
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
    if (!dlg.open) dlg.showModal();
  };
  document.addEventListener("click", (e) => {
    const b = e.target.closest("[data-log]");
    if (b) window.openLog(b);
  });

  form.addEventListener("submit", async (e) => {
    if (e.submitter && e.submitter.value === "cancel") return;
    e.preventDefault();
    const kind = form.kind.value;
    if (kind !== "note" && !form.outcome.value) { toast("Pick an outcome", true); form.outcome.focus(); return; }
    const body = {
      kind, at: form.at.value, outcome: kind === "note" ? "" : form.outcome.value,
      duration_min: ["call", "meeting"].includes(kind) ? form.duration_min.value : "", notes: form.notes.value,
      next_step: form.next_step.value, next_step_at: form.next_step_at.value, by_id: form.by_id.value,
      campaign_id: form.campaign_id.value, stop_sequences: kind !== "note" && form.stop_sequences.checked,
      status: form.status.value,
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

// Lead status: Campaign Status cells and the lead page.
$$("[data-status-of]").forEach((sel) => {
  let before = sel.value;
  sel.addEventListener("change", async () => {
    const closing = ["Converted", "Closed/Lost"].includes(sel.value);
    if (closing && !confirm(`Mark as ${sel.value}? This stops their sequences and their follow-up reminders.`)) { sel.value = before; return; }
    try {
      await api(`/api/leads/${sel.dataset.statusOf}`, { action: "status", status: sel.value });
      before = sel.value;
      $$(`[data-status-of="${sel.dataset.statusOf}"]`).forEach((o) => { o.value = sel.value; });
      $$(`[data-log][data-lead="${sel.dataset.statusOf}"]`).forEach((b) => { b.dataset.status = sel.value; });
      toast("Status saved");
      if (sel.dataset.reload) setTimeout(() => location.reload(), 400);
    } catch (err) { sel.value = before; toast(err.message, true); }
  });
});

// Reminders: snooze by the status's own rhythm.
document.addEventListener("click", async (e) => {
  const b = e.target.closest("[data-snooze]");
  if (!b) return;
  b.disabled = true;
  try {
    const r = await api(`/api/leads/${b.dataset.snooze}`, { action: "snooze" });
    toast(`Snoozed until ${r.until}`);
    setTimeout(() => location.reload(), 500);
  } catch (err) { b.disabled = false; toast(err.message, true); }
});

// Email drafts: preview, approve, skip, hold.
const draftsBadge = (n) => {
  const left = $("#drafts-left");
  if (left) left.textContent = n;
  const badge = $("#nav-drafts .badge");
  if (badge) { if (n) badge.textContent = n; else badge.remove(); }
};
$$("[data-draft-preview]").forEach((b) => b.addEventListener("click", async () => {
  const row = b.closest("tr").nextElementSibling, frame = $("iframe", row);
  row.hidden = !row.hidden;
  b.setAttribute("aria-expanded", String(!row.hidden));
  b.textContent = row.hidden ? "Preview" : "Hide";
  if (!row.hidden && !frame.srcdoc) {
    // Shown from memory in a locked-down frame: nothing in the email can run, and no page load is needed.
    try { frame.srcdoc = await (await fetch(frame.dataset.src)).text(); }
    catch (err) { toast("Couldn't load the preview: " + err.message, true); }
  }
}));
$$("[data-draft-act]").forEach((b) => b.addEventListener("click", async () => {
  const tr = b.closest("tr"), id = tr.dataset.draft, act = b.dataset.draftAct;
  if (b.dataset.ask && !confirm(b.dataset.ask)) return;
  $$("button", tr).forEach((x) => (x.disabled = true));
  try {
    const r = act === "approve" ? await api("/api/drafts/approve", { ids: [id] }) : await api(`/api/drafts/${id}`, { action: act });
    draftsBadge(r.pending);
    toast(act === "approve" ? "Approved. It goes out on the next run inside the send window." : act === "skip" ? "Skipped" : "Back to waiting");
    setTimeout(() => location.reload(), 600);
  } catch (err) { toast(err.message, true); $$("button", tr).forEach((x) => (x.disabled = false)); }
}));
$$("[data-approve-all]").forEach((b) => b.addEventListener("click", async () => {
  if (!confirm(`Approve all ${b.dataset.n} drafts? They're sent inside each campaign's send window, spaced out, within your daily limits.`)) return;
  b.disabled = true;
  try {
    const r = await api("/api/drafts/approve", { all: true, campaign_id: b.dataset.campaign });
    toast(`${r.approved} approved`);
    setTimeout(() => location.reload(), 600);
  } catch (err) { b.disabled = false; toast(err.message, true); }
}));

// Send email / message (the reminder quick action, and on lead pages).
const cdlg = $("#compose-dlg");
if (cdlg) {
  const form = $("#compose-form");
  let src = null;
  const text = () => form.body.value.replaceAll("{{ first_name }}", src.dataset.first || "").trim();
  const syncVia = () => {
    const via = form.via.value;
    $$("[data-via]", form).forEach((el) => { el.hidden = !el.dataset.via.split(" ").includes(via); });
    $("#compose-wa-missing").hidden = !!src.dataset.wa;
    $("#compose-open-wa").disabled = !src.dataset.wa;
    $("#compose-open-li").disabled = !src.dataset.linkedin;
  };
  $$("input[name=via]", form).forEach((r) => r.addEventListener("change", syncVia));
  window.openCompose = (b) => {
    src = b;
    form.reset();
    form.lead.value = b.dataset.lead;
    $("#compose-who").textContent = [b.dataset.name, b.dataset.company, b.dataset.email].filter(Boolean).join(" · ");
    form.via.value = b.dataset.email ? "email" : b.dataset.wa ? "whatsapp" : "linkedin";
    $$("input[name=via]", form).forEach((r) => {
      r.disabled = (r.value === "email" && !b.dataset.email) || (r.value === "linkedin" && !b.dataset.linkedin);
    });
    syncVia();
    cdlg.showModal();
  };
  document.addEventListener("click", (e) => {
    const b = e.target.closest("[data-compose]");
    if (b) window.openCompose(b);
  });
  form.addEventListener("submit", async (e) => {
    if (e.submitter && e.submitter.value === "cancel") return;
    e.preventDefault();
    const send = e.submitter && e.submitter.value === "send";
    $$("button", form).forEach((x) => (x.disabled = true));
    try {
      const r = await api(`/api/leads/${form.lead.value}/compose`, { subject: form.subject.value, body: form.body.value, send });
      cdlg.close();
      if (send) toast("Sending now. It will show in their history in a moment.");
      else {
        toast("Saved in your Outlook Drafts");
        if (r.web_link) window.open(r.web_link, "_blank", "noopener");
      }
      setTimeout(() => location.reload(), 1200);
    } catch (err) { toast(err.message, true); }
    finally { $$("button", form).forEach((x) => (x.disabled = false)); syncVia(); }
  });
  $("#compose-open-wa").addEventListener("click", () => {
    if (!text()) return toast("Write the message first", true);
    window.open(`https://wa.me/${src.dataset.wa}?text=${encodeURIComponent(text())}`, "_blank", "noopener");
  });
  $("#compose-open-li").addEventListener("click", async () => {
    if (text()) { try { await navigator.clipboard.writeText(text()); toast("Copied. Paste it in LinkedIn."); } catch { /* clipboard blocked */ } }
    window.open(src.dataset.linkedin, "_blank", "noopener");
  });
  $("#compose-logged").addEventListener("click", async () => {
    try {
      await api(`/api/leads/${form.lead.value}/activities`, { kind: form.via.value, outcome: "Sent", notes: text(), at: "" });
      cdlg.close();
      toast("Logged");
      setTimeout(() => location.reload(), 500);
    } catch (err) { toast(err.message, true); }
  });
  // A reminder email's "Send email / message" button lands here.
  if (new URLSearchParams(location.search).get("compose")) {
    const b = $("[data-compose]");
    if (b) window.openCompose(b);
  }
}

// Dropdown menus close when you click elsewhere.
document.addEventListener("click", (e) => {
  $$("details.menu[open]").forEach((m) => { if (!m.contains(e.target)) m.open = false; });
});

document.addEventListener("click", async (e) => {
  const b = e.target.closest("[data-del-activity]");
  if (!b || !confirm("Delete this entry? This can't be undone.")) return;
  try { await api(`/api/activities/${b.dataset.delActivity}/delete`); location.reload(); }
  catch (err) { toast(err.message, true); }
});
