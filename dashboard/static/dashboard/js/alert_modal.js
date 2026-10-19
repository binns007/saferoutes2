/**
 * Shared popup-alert modal. Used by route_detail.js whenever a deviation
 * (simulated or from live GPS tracking) needs to interrupt the user with
 * reroute / continue / dismiss choices.
 *
 * The alert is shown to the user. SafeRoutes contacts no one on its own: the
 * user's emergency contact is emailed only if the user presses the button, or
 * opted in to auto-escalation and lets the visible countdown run out.
 * Authorities are never contacted.
 */
window.SafeRoutesAlertModal = (function () {
  function getCsrfToken() {
    const input = document.querySelector("#sr-csrf-form input[name=csrfmiddlewaretoken]");
    return input ? input.value : "";
  }

  // Generates a short beep pattern with the Web Audio API -- no external
  // audio file/network request needed. Critical alerts get three sharper,
  // higher-pitched beeps; warnings get two calmer ones.
  function playAlertSound(severity) {
    try {
      const Ctx = window.AudioContext || window.webkitAudioContext;
      if (!Ctx) return;
      const ctx = new Ctx();
      const isCritical = severity === "critical";
      const beepCount = isCritical ? 3 : 2;
      const freq = isCritical ? 880 : 660;
      let t = ctx.currentTime;

      for (let i = 0; i < beepCount; i++) {
        const osc = ctx.createOscillator();
        const gain = ctx.createGain();
        osc.type = "sine";
        osc.frequency.setValueAtTime(freq, t);
        gain.gain.setValueAtTime(0, t);
        gain.gain.linearRampToValueAtTime(0.25, t + 0.02);
        gain.gain.linearRampToValueAtTime(0, t + 0.22);
        osc.connect(gain).connect(ctx.destination);
        osc.start(t);
        osc.stop(t + 0.25);
        t += 0.32;
      }

      setTimeout(() => ctx.close().catch(() => {}), beepCount * 320 + 200);
    } catch (e) {
      // never let a sound failure block the visible alert
    }

    if (navigator.vibrate) {
      navigator.vibrate(severity === "critical" ? [200, 100, 200, 100, 200] : [150, 80, 150]);
    }
  }

  let countdownTimer = null;

  function close(root) {
    if (countdownTimer) { clearInterval(countdownTimer); countdownTimer = null; }
    root.innerHTML = "";
    document.body.classList.remove("sr-modal-open");
  }

  function show(opts) {
    const root = document.getElementById("sr-modal-root");
    if (!root) return;
    root.innerHTML = "";
    document.body.classList.add("sr-modal-open");

    playAlertSound(opts.severity);

    const backdrop = document.createElement("div");
    backdrop.className = "sr-modal-backdrop";

    const sheet = document.createElement("div");
    sheet.className = "sr-modal-sheet" + (opts.severity === "critical" ? " sr-modal-critical" : "");

    const badge = document.createElement("span");
    badge.className = "sr-modal-badge";
    badge.textContent = opts.severity === "critical" ? "Critical" : "Warning";

    const heading = document.createElement("h3");
    heading.textContent = opts.title || "Route deviation detected";

    const message = document.createElement("p");
    message.className = "sr-modal-message";
    message.textContent = opts.message || "";

    sheet.appendChild(badge);
    sheet.appendChild(heading);
    sheet.appendChild(message);

    if (opts.subtext) {
      const subtext = document.createElement("p");
      subtext.className = "sr-modal-subtext";
      subtext.textContent = opts.subtext;
      sheet.appendChild(subtext);
    }

    const actions = document.createElement("div");
    actions.className = "sr-modal-actions";

    const rerouteBtn = document.createElement("button");
    rerouteBtn.type = "button";
    rerouteBtn.className = "btn";
    rerouteBtn.textContent = "Reroute me";
    rerouteBtn.addEventListener("click", () => {
      close(root);
      if (opts.onReroute) opts.onReroute();
    });

    const continueBtn = document.createElement("button");
    continueBtn.type = "button";
    continueBtn.className = "btn btn-ghost";
    continueBtn.textContent = "I'm okay, continue";
    continueBtn.addEventListener("click", () => {
      close(root);
      if (opts.onContinue) opts.onContinue();
    });

    const dismissBtn = document.createElement("button");
    dismissBtn.type = "button";
    dismissBtn.className = "sr-btn-dismiss";
    dismissBtn.textContent = "Dismiss";
    dismissBtn.addEventListener("click", () => {
      close(root);
      if (opts.onDismiss) opts.onDismiss();
    });

    actions.appendChild(rerouteBtn);
    actions.appendChild(continueBtn);

    // opt-in emergency contact (name only: the address never reaches the browser)
    if (opts.contact && opts.onNotify) {
      const notifyBtn = document.createElement("button");
      notifyBtn.type = "button";
      notifyBtn.className = "btn sr-btn-notify";
      notifyBtn.textContent = `Notify ${opts.contact.name}`;
      notifyBtn.addEventListener("click", () => {
        close(root);
        opts.onNotify(false);
      });
      actions.appendChild(notifyBtn);

      if (opts.contact.auto_escalate) {
        let left = opts.autoEscalateSeconds || 60;
        const countdown = document.createElement("p");
        countdown.className = "sr-modal-countdown";
        countdown.setAttribute("role", "timer");
        const render = () => {
          countdown.textContent =
            `No response? ${opts.contact.name} will be notified in ${left}s. Choose any option to cancel.`;
        };
        render();
        sheet.appendChild(countdown);
        countdownTimer = setInterval(() => {
          left -= 1;
          if (left <= 0) {
            close(root);
            opts.onNotify(true);
          } else {
            render();
          }
        }, 1000);
      }
    }

    actions.appendChild(dismissBtn);
    sheet.appendChild(actions);

    root.appendChild(backdrop);
    root.appendChild(sheet);
  }

  function postAction(url, choice, extra) {
    const payload = Object.assign({}, extra || {});
    if (choice) payload.choice = choice;
    return fetch(url, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "X-CSRFToken": getCsrfToken(),
      },
      body: Object.keys(payload).length ? JSON.stringify(payload) : undefined,
    });
  }

  return { show, postAction };
})();
