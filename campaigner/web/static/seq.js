// Sequence editor: build steps, live-preview the focused one against a real lead, save as JSON.
(() => {
  const root = document.getElementById("seq");
  if (!root) return;
  const cid = root.dataset.campaign;
  const CH = JSON.parse(root.dataset.channels);
  const COND = JSON.parse(root.dataset.conditions);
  const VARS = JSON.parse(root.dataset.vars);
  let steps = JSON.parse(document.getElementById("seq-data").textContent).map((s) => ({
    channel: s.channel, delay_days: s.delay_days, condition: s.condition,
    subject: s.subject || "", body: s.body || "", in_thread: !!s.in_thread,
  }));
  let focus = 0, dirty = false, lastField = null, timer = null;
  const list = $("#steps"), out = $("#preview-out"), leadSel = $("#preview-lead"), errs = $("#seq-errors");

  function h(tag, attrs = {}, ...kids) {
    const el = document.createElement(tag);
    for (const [k, v] of Object.entries(attrs)) {
      if (k.startsWith("on")) el.addEventListener(k.slice(2), v);
      else if (k === "class") el.className = v;
      else if (v === true) el.setAttribute(k, "");
      else if (v !== false && v != null) el.setAttribute(k, v);
    }
    kids.flat().forEach((c) => c != null && el.append(c));
    return el;
  }
  const select = (opts, val, on) =>
    h("select", { onchange: on }, Object.entries(opts).map(([k, v]) => h("option", { value: k, selected: k === val }, v)));

  function touch() { dirty = true; $("#seq-save").classList.add("primary"); }
  function set(i, key, val, rerender) {
    steps[i][key] = val; touch();
    if (rerender) render(); else schedulePreview();
  }

  function render() {
    list.innerHTML = "";
    let seenEmail = false;
    steps.forEach((s, i) => {
      const firstEmail = s.channel === "email" && !seenEmail;
      if (s.channel === "email") seenEmail = true;
      list.append(h("div", { class: "wait" }, "↓ wait ",
        h("input", { class: "delay", type: "number", min: 0, step: 0.5, value: s.delay_days,
          oninput: (e) => set(i, "delay_days", parseFloat(e.target.value) || 0) }),
        i === 0 ? " days after enrolling (or start date)" : " days after the previous step"));

      const body = [];
      if (s.channel === "email") {
        body.push(h("input", {
          placeholder: firstEmail || !s.in_thread ? "Subject (required)" : "Subject: leave blank to reply in the same thread",
          value: s.subject, oninput: (e) => set(i, "subject", e.target.value), onfocus: (e) => (lastField = e.target),
        }));
        if (!firstEmail) body.push(h("label", { class: "meta" },
          h("input", { type: "checkbox", checked: s.in_thread, onchange: (e) => set(i, "in_thread", e.target.checked, true) }),
          " reply in the same thread as the previous email"));
      }
      if (s.channel === "li_view") {
        body.push(h("div", { class: "meta" }, "Task: open their profile so your visit shows up. No message."));
      } else {
        body.push(h("textarea", {
          rows: s.channel === "email" ? 6 : 3, value: s.body,
          placeholder: s.channel === "li_connect" ? "Invite note (optional, max 300 chars)" : "Message",
          oninput: (e) => set(i, "body", e.target.value), onfocus: (e) => (lastField = e.target),
        }, s.body));
        body.push(h("div", { class: "vars" }, VARS.map((v) => h("button", { type: "button",
          onclick: () => insertVar(v, i) }, `{{ ${v} }}`))));
      }
      if (s.channel === "li_message") body.push(h("div", { class: "meta" },
        "Only sends once connected. Waits up to 7 days for the invite to be accepted, then skips."));

      const card = h("div", { class: "step" + (i === focus ? " focus" : ""), onfocusin: () => setFocus(i), onclick: () => setFocus(i) },
        h("div", { class: "step-row" },
          h("span", { class: "step-num" }, `Step ${i + 1}`),
          select(CH, s.channel, (e) => set(i, "channel", e.target.value, true)),
          s.channel === "li_message" ? null : select(COND, s.condition, (e) => set(i, "condition", e.target.value)),
          h("span", { class: "step-tools" },
            h("button", { class: "btn sm ghost", type: "button", title: "Move up", disabled: i === 0, onclick: () => move(i, -1) }, "↑"),
            h("button", { class: "btn sm ghost", type: "button", title: "Move down", disabled: i === steps.length - 1, onclick: () => move(i, 1) }, "↓"),
            h("button", { class: "btn sm danger", type: "button", title: "Remove step", onclick: () => remove(i) }, "✕"))),
        body);
      list.append(card);
    });
    if (!steps.length) list.append(h("div", { class: "empty" }, "No steps. Add one below."));
    schedulePreview();
  }

  function setFocus(i) {
    if (focus === i) return;
    focus = i;
    $$(".step", list).forEach((el, j) => el.classList.toggle("focus", j === i));
    schedulePreview();
  }
  function move(i, d) { [steps[i], steps[i + d]] = [steps[i + d], steps[i]]; focus = i + d; touch(); render(); }
  function remove(i) { steps.splice(i, 1); focus = Math.max(0, Math.min(focus, steps.length - 1)); touch(); render(); }
  function insertVar(v, i) {
    const f = lastField && list.contains(lastField) ? lastField : $$(".step", list)[i].querySelector("textarea");
    if (!f) return;
    const tok = `{{ ${v} }}`, a = f.selectionStart ?? f.value.length;
    f.value = f.value.slice(0, a) + tok + f.value.slice(f.selectionEnd ?? a);
    f.focus(); f.selectionStart = f.selectionEnd = a + tok.length;
    f.dispatchEvent(new Event("input"));
  }

  function schedulePreview() { clearTimeout(timer); timer = setTimeout(preview, 250); }
  async function preview() {
    const s = steps[focus];
    out.innerHTML = "";
    if (!s) return;
    $("#preview-title").textContent = `Step ${focus + 1} · ${CH[s.channel]}`;
    if (s.channel === "li_view") { out.append(h("div", { class: "meta" }, "No message. The task opens their profile.")); return; }
    const firstEmail = steps.find((x) => x.channel === "email");
    const priorEmail = steps.slice(0, focus).some((x) => x.channel === "email");
    try {
      const r = await api("/api/preview", {
        campaign_id: cid, lead_id: leadSel.value || null, step: s,
        first_subject: firstEmail ? firstEmail.subject : "", in_thread_followup: priorEmail && s.in_thread,
      });
      if (r.error) { out.append(h("div", { class: "errors" }, "Template error: " + r.error)); return; }
      if (r.html != null) {
        const mail = h("div", { class: "mail" }, h("div", { class: "subj" }, r.subject || "(no subject)"));
        const bodyEl = h("div");
        bodyEl.innerHTML = r.html; // server-rendered from your template; lead values are escaped
        mail.append(bodyEl);
        out.append(mail);
      } else {
        out.append(h("div", { class: "li-bubble" }, r.text || "(no note)"));
        if (s.channel === "li_connect") out.append(h("div", { class: "count" + (r.chars > 200 ? " warn" : "") },
          `${r.chars}/300 characters` + (r.chars > 200 ? " · free accounts cap at 200" : "")));
      }
    } catch (err) { out.append(h("div", { class: "errors" }, err.message)); }
  }

  $$("[data-add]").forEach((b) => b.addEventListener("click", () => {
    steps.push({ channel: b.dataset.add, delay_days: steps.length ? 2 : 0, condition: "always",
      subject: "", body: "", in_thread: true });
    focus = steps.length - 1; touch(); render();
  }));
  leadSel.addEventListener("change", preview);
  $("#seq-save").addEventListener("click", async () => {
    errs.innerHTML = "";
    try {
      await api(`/api/campaigns/${cid}/steps`, { steps });
      dirty = false; $("#seq-save").classList.remove("primary"); toast("Sequence saved");
    } catch (err) {
      err.message.split("; ").forEach((m) => errs.append(h("li", {}, m)));
      toast("Not saved: fix the errors listed", true);
    }
  });
  addEventListener("beforeunload", (e) => { if (dirty) { e.preventDefault(); e.returnValue = ""; } });
  render();
})();
