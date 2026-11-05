// Sequence builder: the sequence as a vertical flowchart. Click a step to edit it in the inspector, drag a step
// onto a connector to move it, and use "+" on any connector to insert a step there. Each step carries its live
// counts from the server, so they stay attached when steps are reordered.
(() => {
  const root = document.getElementById("seq");
  if (!root) return;
  const cid = root.dataset.campaign;
  const CH = JSON.parse(root.dataset.channels);
  const COND = JSON.parse(root.dataset.conditions);
  const VARS = JSON.parse(root.dataset.vars);
  const LEADS = JSON.parse(root.dataset.leads);
  const ENGAGE = root.dataset.engage === "1";
  const DUMMY = root.dataset.dummy === "1";  // test mode: every wait scaled so the run finishes within 30 minutes
  const DUMMY_MINUTES = 25;
  const enrolled = +root.dataset.enrolled, pending = +root.dataset.pending;
  const flow = $("#flow"), insp = $("#inspector"), errList = $("#seq-errors");

  let steps = JSON.parse(document.getElementById("seq-data").textContent).map((s) => ({
    channel: s.channel, delay_days: s.delay_days, condition: s.condition, subject: s.subject || "",
    body: s.body || "", in_thread: !!s.in_thread,
    attachments: typeof s.attachments === "string" ? JSON.parse(s.attachments || "[]") : (s.attachments || []),
    stats: { done: s.done || 0, queued: s.queued || 0, waiting: s.waiting || 0, replies: s.replies || 0 },
  }));
  let sel = steps.length ? 0 : -1, dirty = false, dragFrom = null, timer = null, lastField = null, menu = null;
  let previewLead = LEADS.length ? String(LEADS[0].id) : "";
  let errSteps = new Set();

  const ICONS = {
    email: '<path d="M3 6h18v12H3z"/><path d="M3 7l9 6 9-6"/>',
    li_connect: '<circle cx="9" cy="8" r="3.5"/><path d="M2.5 20c.8-3.6 3.4-5.5 6.5-5.5s5.7 1.9 6.5 5.5"/><path d="M19 8v6M16 11h6"/>',
    li_message: '<path d="M4 5h16v11H9l-5 4z"/><path d="M8 9.5h8M8 12.5h5"/>',
    li_view: '<path d="M2 12s3.6-6.5 10-6.5S22 12 22 12s-3.6 6.5-10 6.5S2 12 2 12z"/><circle cx="12" cy="12" r="3"/>',
    start: '<path d="M8 5v14l11-7z"/>',
    end: '<path d="M6 6h12v12H6z"/>',
  };

  function h(tag, attrs = {}, ...kids) {
    const el = document.createElement(tag);
    for (const [k, v] of Object.entries(attrs)) {
      if (k.startsWith("on")) el.addEventListener(k.slice(2), v);
      else if (k === "class") el.className = v;
      else if (v === true) el.setAttribute(k, "");
      else if (v !== false && v != null) el.setAttribute(k, v);
    }
    kids.flat().forEach((c) => c != null && c !== false && el.append(c));
    return el;
  }
  function icon(name) {
    const s = h("span", { class: "ic", "aria-hidden": "true" });
    s.innerHTML = `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round">${ICONS[name]}</svg>`;
    return s;
  }
  const select = (opts, val, on) =>
    h("select", { onchange: on }, Object.entries(opts).map(([k, v]) => h("option", { value: k, selected: k === val }, v)));
  const fmt = (n) => String(Math.round((+n || 0) * 10) / 10);
  const testScale = () => {
    const total = steps.reduce((a, s) => a + (+s.delay_days || 0), 0);
    return total ? Math.min(1, DUMMY_MINUTES / (total * 1440)) : 0;
  };
  const testMins = (days) => {
    const m = (+days || 0) * 1440 * testScale();
    return m < 1 ? `${Math.round(m * 60)}s` : `${Math.round(m * 10) / 10}m`;
  };
  const kb = (n) => n > 1048576 ? `${(n / 1048576).toFixed(1)} MB` : `${Math.max(1, Math.round(n / 1024))} KB`;
  const isLI = (ch) => ch !== "email";
  const gated = (s) => s.channel === "li_message" || s.condition !== "always";
  const priorEmail = (i) => steps.slice(0, i).some((x) => x.channel === "email");
  const waitsForConnect = (s) => s.channel === "li_message" && s.condition === "always";
  const BRANCH_Q = { if_connected: "Connected?", if_not_connected: "Not connected?",
                     if_was_connected: "Connected at start?", if_was_invited: "Needed an invite?" };
  const needsConnect = (i) => waitsForConnect(steps[i]) && !steps.slice(0, i).some((x) => x.channel === "li_connect");

  function summary(s, i) {
    if (s.channel === "li_view") return "Visit their profile";
    if (s.channel === "email") {
      if (s.subject) return s.subject;
      return priorEmail(i) && s.in_thread ? "↳ reply in the same thread" : "(subject needed)";
    }
    const first = (s.body || "").split("\n").find((l) => l.trim());
    return first || (s.channel === "li_connect" ? "Invite without a note" : "(message needed)");
  }

  function markDirty() {
    dirty = true;
    $("#seq-save").classList.add("primary");
    $("#seq-dirty").textContent = "Unsaved changes";
  }

  // ---------- flowchart ----------

  function connector(index) {
    const last = index === steps.length;
    const el = h("div", { class: "fconn" + (last ? " last" : ""), "data-index": index },
      h("span", { class: "fline" }),
      last ? null : h("button", {
        class: "fwait", type: "button", title: "Wait before the next step",
        onclick: () => selectStep(index, "wait"),
      }, (+steps[index].delay_days || 0) > 0
        ? `wait ${fmt(steps[index].delay_days)}d` + (DUMMY ? ` · test ${testMins(steps[index].delay_days)}` : "")
        : "no wait"),
      h("button", {
        class: "fadd", type: "button", title: "Insert a step here", "aria-label": `Insert a step at position ${index + 1}`,
        onclick: (e) => { e.stopPropagation(); openMenu(e.currentTarget, index); },
      }, "+"));
    el.addEventListener("dragover", (e) => {
      if (dragFrom === null || index === dragFrom || index === dragFrom + 1) return;
      e.preventDefault();
      el.classList.add("drop");
    });
    el.addEventListener("dragleave", () => el.classList.remove("drop"));
    el.addEventListener("drop", (e) => {
      e.preventDefault();
      el.classList.remove("drop");
      if (dragFrom !== null) moveTo(dragFrom, index);
    });
    return el;
  }

  function node(s, i, day) {
    const st = s.stats;
    const chips = [
      st.done && `✓ ${st.done} done`,
      st.queued && `${st.queued} in queue`,
      st.waiting && `${st.waiting} up next`,
      st.replies && `↩ ${st.replies} replied`,
    ].filter(Boolean);
    const el = h("div", {
      class: `fnode step ${isLI(s.channel) ? "ch-li" : "ch-email"}${i === sel ? " sel" : ""}${errSteps.has(i) ? " err" : ""}`,
      draggable: "true", tabindex: "0", role: "button",
      "aria-label": `Step ${i + 1}: ${CH[s.channel]}, day ${fmt(day)}`,
      onclick: () => selectStep(i),
      onkeydown: (e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); selectStep(i); } },
    },
      h("span", { class: "fnum" }, String(i + 1)),
      h("div", { class: "ficon" }, icon(s.channel)),
      h("div", { class: "fbody" },
        h("div", { class: "ftitle" }, h("span", {}, CH[s.channel]), h("span", { class: "fday" }, `Day ${fmt(day)}${waitsForConnect(s) ? "+" : ""}`)),
        h("div", { class: "fsub" }, summary(s, i)),
        s.channel === "email" && s.attachments.length
          ? h("div", { class: "fstats" }, h("span", {}, `${s.attachments.length} attachment${s.attachments.length > 1 ? "s" : ""}`)) : null,
        needsConnect(i) ? h("div", { class: "fwarn" },
          "⚠ No Connect step before this. LinkedIn only allows messages to connections, so leads who aren't already connected wait 7 days here, then skip.") : null,
        chips.length ? h("div", { class: "fstats" }, chips.map((c) => h("span", {}, c))) : null));
    el.addEventListener("dragstart", (e) => {
      dragFrom = i;
      e.dataTransfer.effectAllowed = "move";
      e.dataTransfer.setData("text/plain", String(i));
      requestAnimationFrame(() => { el.classList.add("dragging"); flow.classList.add("is-dragging"); });
    });
    el.addEventListener("dragend", () => { dragFrom = null; el.classList.remove("dragging"); flow.classList.remove("is-dragging"); });
    return el;
  }

  function branch(s, i, day) {
    const q = BRANCH_Q[s.condition] || "Connected?";
    const diamond = h("div", { class: "fdiamond" });
    diamond.innerHTML = '<svg viewBox="0 0 172 56" preserveAspectRatio="none" aria-hidden="true"><polygon points="86,1 171,28 86,55 1,28"/></svg>';
    diamond.append(h("span", {}, q));
    return h("div", { class: "fbranch" },
      diamond,
      h("div", { class: "fyes" }, h("span", {}, "yes")),
      node(s, i, day),
      h("div", { class: "fbypass" }, h("span", {}, waitsForConnect(s) ? "no → wait ≤7d, then skip" : "no → skip")),
      h("div", { class: "fbypass-ret" }));
  }

  function render() {
    flow.innerHTML = "";
    let d = 0;
    flow.append(h("div", { class: "fnode cap" },
      h("div", { class: "ficon" }, icon("start")),
      h("div", { class: "fbody" }, h("div", { class: "ftitle" }, "Lead enrolled"),
        h("div", { class: "fsub" }, enrolled ? `${enrolled} leads · ${pending} not started yet` : "Import leads to start"))));
    steps.forEach((s, i) => {
      d += +s.delay_days || 0;
      flow.append(connector(i), gated(s) ? branch(s, i, d) : node(s, i, d));
    });
    flow.append(connector(steps.length));
    flow.append(h("div", { class: "fnode cap" },
      h("div", { class: "ficon" }, icon("end")),
      h("div", { class: "fbody" }, h("div", { class: "ftitle" }, "Sequence complete"),
        h("div", { class: "fsub" }, "Still watched 30 days for late replies"))));
    if (ENGAGE && steps.length) {
      flow.append(h("div", { class: "flane", title: "Runs alongside the sequence; set in campaign Settings" },
        h("span", {}, "Weekly like + comment on recent posts · until reply")));
    }
    if (!steps.length) flow.querySelector(".fconn .fadd").classList.add("pulse");
  }

  // ---------- add / move / remove ----------

  function openMenu(btn, index) {
    closeMenu();
    menu = h("div", { class: "fmenu", role: "menu" },
      Object.entries(CH).map(([k, v]) => h("button", {
        type: "button", role: "menuitem", class: isLI(k) ? "ch-li" : "ch-email",
        onclick: (e) => { e.stopPropagation(); insertStep(index, k); },
      }, icon(k), v)));
    btn.parentElement.append(menu);
    menu.querySelector("button").focus();
  }
  function closeMenu() { if (menu) { menu.remove(); menu = null; } }
  document.addEventListener("click", (e) => { if (menu && !menu.contains(e.target)) closeMenu(); });
  document.addEventListener("keydown", (e) => { if (e.key === "Escape") closeMenu(); });

  function insertStep(index, ch) {
    closeMenu();
    steps.splice(index, 0, { channel: ch, delay_days: index === 0 ? 0 : 2, condition: "always", subject: "",
      body: "", in_thread: true, stats: { done: 0, queued: 0, waiting: 0, replies: 0 } });
    sel = index;
    errSteps.clear();
    markDirty();
    render();
    inspector();
  }
  function moveTo(from, to) {
    if (to > from) to -= 1;
    if (to === from) return;
    const [s] = steps.splice(from, 1);
    steps.splice(to, 0, s);
    sel = to;
    errSteps.clear();
    markDirty();
    render();
    inspector();
  }
  function removeStep(i) {
    steps.splice(i, 1);
    sel = Math.min(i, steps.length - 1);
    errSteps.clear();
    markDirty();
    render();
    inspector();
  }

  // ---------- inspector ----------

  function selectStep(i, focus) {
    sel = i;
    $$(".fnode.step", flow).forEach((el, j) => el.classList.toggle("sel", j === i));
    inspector();
    if (focus === "wait") $("#f-wait")?.focus();
  }
  function set(key, val, rerenderInspector) {
    steps[sel][key] = val;
    markDirty();
    render();
    if (rerenderInspector) inspector();
    else schedulePreview();
  }
  const field = (label, control, hint) => h("label", { class: "f" }, label, control, hint ? h("span", { class: "meta" }, hint) : null);

  function insertVar(v) {
    const f = lastField && insp.contains(lastField) ? lastField : $("#f-body");
    if (!f) return;
    const tok = `{{ ${v} }}`, a = f.selectionStart ?? f.value.length;
    f.value = f.value.slice(0, a) + tok + f.value.slice(f.selectionEnd ?? a);
    f.focus();
    f.selectionStart = f.selectionEnd = a + tok.length;
    f.dispatchEvent(new Event("input"));
  }

  function inspector() {
    insp.innerHTML = "";
    const s = steps[sel];
    if (!s) {
      insp.append(h("div", { class: "card-head" }, h("h2", {}, "Step")),
        h("p", { class: "meta" }, steps.length ? "Click a step in the flow to edit it." : "Add a first step with the + under “Lead enrolled”."));
      return;
    }
    const i = sel, firstEmail = s.channel === "email" && !priorEmail(i);
    const counter = h("div", { class: "count" });
    const updateCount = (n) => {
      counter.textContent = `${n}/300` + (n > 200 ? " · free LinkedIn accounts cap notes at 200" : "");
      counter.classList.toggle("warn", n > 200);
    };
    insp.append(
      h("div", { class: "card-head" },
        h("h2", {}, `Step ${i + 1} of ${steps.length}`),
        h("span", { class: "step-tools" },
          h("button", { class: "btn sm ghost", type: "button", title: "Move up", "aria-label": "Move step up", disabled: i === 0, onclick: () => moveTo(i, i - 1) }, "↑"),
          h("button", { class: "btn sm ghost", type: "button", title: "Move down", "aria-label": "Move step down", disabled: i === steps.length - 1, onclick: () => moveTo(i, i + 2) }, "↓"),
          h("button", { class: "btn sm danger", type: "button", title: "Remove step", onclick: () => removeStep(i) }, "Remove"))),
      h("div", { class: "stack-y", style: "gap:10px" },
        field("Type", select(CH, s.channel, (e) => set("channel", e.target.value, true))),
        field("Wait before this step (days)",
          h("input", { id: "f-wait", type: "number", min: 0, step: 0.5, value: s.delay_days,
            oninput: (e) => set("delay_days", parseFloat(e.target.value) || 0) }),
          i === 0 ? "Counted from enrollment, or the campaign's start date." : "Counted from when the previous step was done or skipped."),
        s.channel === "li_message"
          ? field("Run", select(
              { always: "Always (if not connected yet, wait up to 7 days for them to accept)",
                if_connected: "Only if connected (otherwise skip right away)",
                if_was_connected: COND.if_was_connected, if_was_invited: COND.if_was_invited + " (and have accepted)" },
              s.condition === "if_not_connected" ? "always" : s.condition, (e) => set("condition", e.target.value)),
              "LinkedIn only allows messages to connections. “Connected at the start” vs “needed an invite” is decided once, after the Connect step, so you can give each group its own steps.")
          : field("Run", select(s.channel === "li_connect"
                ? { always: COND.always, if_not_connected: COND.if_not_connected } : COND,
              s.condition, (e) => set("condition", e.target.value)),
              s.channel === "li_connect" ? "Already-connected leads skip this step automatically." : null),
        s.channel === "email" ? field(firstEmail || !s.in_thread ? "Subject" : "Subject (blank = reply in the same thread)",
          h("input", { value: s.subject, placeholder: firstEmail ? "Required: starts the thread" : "",
            oninput: (e) => set("subject", e.target.value), onfocus: (e) => (lastField = e.target) })) : null,
        s.channel === "email" && !firstEmail ? h("label", { class: "days" },
          h("input", { type: "checkbox", checked: s.in_thread, onchange: (e) => set("in_thread", e.target.checked, true) }),
          "Reply in the same thread as the previous email") : null,
        s.channel === "li_view" ? h("p", { class: "meta" }, "Task: open their profile so your visit shows up. No message.") : [
          toolbar(s.channel),
          field(s.channel === "email" ? "Message" : s.channel === "li_connect" ? "Invite note (optional)" : "Message",
            h("textarea", {
              id: "f-body", rows: s.channel === "email" ? 8 : 4,
              oninput: (e) => { set("body", e.target.value); if (s.channel === "li_connect") updateCount(e.target.value.length); },
              onfocus: (e) => (lastField = e.target),
            }, s.body)),
          s.channel === "li_connect" ? counter : null,
          h("div", { class: "vars" }, VARS.map((v) => h("button", { type: "button", onclick: () => insertVar(v) }, `{{ ${v} }}`))),
          h("p", { class: "meta" }, s.channel === "email"
            ? "**bold**, *italic*, lines starting “- ” or “1. ” for lists, [text](https://…) for links. A blank line starts a paragraph. Fallback: {{ title | default('there', true) }}."
            : "LinkedIn messages are plain text: **bold** and *italic* become Unicode bold/italic letters, “- ” lines become • bullets. Use sparingly (screen readers read styled letters awkwardly)."),
          s.channel === "email" ? attachmentsField(s) : null,
        ]),
      h("div", { class: "card-head", style: "margin:18px 0 8px" }, h("h2", {}, "Preview")),
      field("Render for", h("select", { onchange: (e) => { previewLead = e.target.value; preview(); } },
        LEADS.length ? LEADS.map((l) => h("option", { value: l.id, selected: String(l.id) === previewLead }, l.label))
          : h("option", { value: "" }, "Sample lead (Jane Doe, Acme Corp)"))),
      h("div", { id: "preview-out", style: "margin-top:10px" }));
    if (s.channel === "li_connect") updateCount((s.body || "").length);
    schedulePreview();
  }

  function toolbar(channel) {
    const wrap = (before, after = before, placeholder = "text") => {
      const f = $("#f-body");
      if (!f) return;
      const a = f.selectionStart, b = f.selectionEnd, sel = f.value.slice(a, b) || placeholder;
      f.value = f.value.slice(0, a) + before + sel + after + f.value.slice(b);
      f.focus();
      f.selectionStart = a + before.length;
      f.selectionEnd = a + before.length + sel.length;
      f.dispatchEvent(new Event("input"));
    };
    const lines = (prefix) => {
      const f = $("#f-body");
      if (!f) return;
      let a = f.value.lastIndexOf("\n", f.selectionStart - 1) + 1, b = f.value.indexOf("\n", f.selectionEnd);
      if (b < 0) b = f.value.length;
      const block = f.value.slice(a, b).split("\n").map((l, i) => (prefix === "1." ? `${i + 1}. ` : "- ") + l.replace(/^\s*([-*•]|\d+[.)])\s+/, "")).join("\n");
      f.value = f.value.slice(0, a) + block + f.value.slice(b);
      f.focus();
      f.dispatchEvent(new Event("input"));
    };
    const link = () => {
      const url = prompt("Link address (https://…)");
      if (url && /^https?:\/\//.test(url)) wrap("[", `](${url})`, "link text");
    };
    const b = (label, title, on) => h("button", { type: "button", class: "btn sm ghost", title, onclick: on }, label);
    return h("div", { class: "fmt-bar", role: "toolbar", "aria-label": "Formatting" },
      b("B", "Bold (**text**)", () => wrap("**")), b("I", "Italic (*text*)", () => wrap("*")),
      b("• List", "Bulleted list", () => lines("-")), b("1. List", "Numbered list", () => lines("1.")),
      channel === "email" ? b("Link", "Link ([text](url))", link) : null);
  }

  function attachmentsField(s) {
    const list = h("div", { class: "att-list" }, s.attachments.map((a, j) => h("span", { class: "att" },
      h("a", { href: `/api/attachments/${a.id}?name=${encodeURIComponent(a.name)}`, target: "_blank" }, a.name),
      h("span", { class: "meta" }, ` ${kb(a.size)} `),
      h("button", { type: "button", class: "btn sm ghost", title: `Remove ${a.name}`, "aria-label": `Remove ${a.name}`,
        onclick: () => { s.attachments.splice(j, 1); markDirty(); render(); inspector(); } }, "✕"))));
    const input = h("input", { type: "file", multiple: true, style: "display:none", onchange: async (e) => {
      for (const file of e.target.files) {
        const fd = new FormData();
        fd.append("file", file);
        try {
          const r = await fetch("/api/attachments", { method: "POST", body: fd });
          const data = await r.json();
          if (!r.ok) throw new Error(data.detail || r.statusText);
          s.attachments.push(data);
          markDirty();
        } catch (err) { toast(err.message, true); }
      }
      render(); inspector();
    } });
    return h("div", { class: "f" }, "Attachments (up to 3 MB each, sent with every email from this step)", list,
      h("div", {}, input, h("button", { type: "button", class: "btn sm", onclick: () => input.click() }, "+ Attach files")));
  }

  function schedulePreview() { clearTimeout(timer); timer = setTimeout(preview, 250); }
  async function preview() {
    const out = $("#preview-out"), s = steps[sel];
    if (!out || !s) return;
    if (s.channel === "li_view") { out.replaceChildren(h("div", { class: "meta" }, "No message. The task opens their profile.")); return; }
    const firstEmail = steps.find((x) => x.channel === "email");
    try {
      const r = await api("/api/preview", {
        campaign_id: cid, lead_id: previewLead || null,
        step: { channel: s.channel, subject: s.subject, body: s.body },
        first_subject: firstEmail ? firstEmail.subject : "", in_thread_followup: priorEmail(sel) && s.in_thread,
      });
      if (r.error) { out.replaceChildren(h("div", { class: "errors" }, "Template error: " + r.error)); return; }
      if (r.html != null) {
        const body = h("div");
        body.innerHTML = r.html; // server-rendered from your template; lead values are escaped
        out.replaceChildren(h("div", { class: "mail" }, h("div", { class: "subj" }, r.subject || "(no subject)"), body),
          s.attachments.length ? h("div", { class: "meta", style: "margin-top:6px" }, "Attached: " + s.attachments.map((a) => a.name).join(", ")) : "");
      } else {
        out.replaceChildren(h("div", { class: "li-bubble" }, r.text || "(no note)"));
      }
    } catch (err) { out.replaceChildren(h("div", { class: "errors" }, err.message)); }
  }

  // ---------- save ----------

  $("#seq-save").addEventListener("click", async () => {
    errList.innerHTML = "";
    errSteps = new Set();
    try {
      await api(`/api/campaigns/${cid}/steps`, {
        steps: steps.map(({ stats, ...s }) => s),
      });
      dirty = false;
      $("#seq-save").classList.remove("primary");
      $("#seq-dirty").textContent = "Saved";
      toast("Sequence saved");
      render();
    } catch (err) {
      err.message.split("; ").forEach((m) => {
        errList.append(h("li", {}, m));
        const n = /^Step (\d+)/.exec(m);
        if (n) errSteps.add(+n[1] - 1);
      });
      render();
      if (errSteps.size) selectStep([...errSteps][0]);
      toast("Not saved: fix the highlighted steps", true);
    }
  });
  addEventListener("beforeunload", (e) => { if (dirty) { e.preventDefault(); e.returnValue = ""; } });

  render();
  inspector();
  const canvas = flow.parentElement;  // start centred when the flow is wider than the canvas
  canvas.scrollLeft = (canvas.scrollWidth - canvas.clientWidth) / 2;
})();
