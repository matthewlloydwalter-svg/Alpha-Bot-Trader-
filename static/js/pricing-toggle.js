/**
 * Shared Weekly / Monthly / Annually pricing toggle.
 * Default interval: month. Annually shows a "Save 20%" badge.
 *
 * Markup contract:
 *  - .billing-toggle with buttons[data-interval=week|month|year]
 *  - .save-badge (hidden unless year)
 *  - .tier[data-plan] .price-amount / .price-period
 *  - Optional .upgrade-btn[data-plan] with data-price-week/month/year
 */
(function (global) {
  const INTERVALS = ["week", "month", "year"];

  function applyInterval(root, interval, plansByKey) {
    const iv = INTERVALS.includes(interval) ? interval : "month";
    root.querySelectorAll(".billing-toggle [data-interval]").forEach((btn) => {
      btn.classList.toggle("active", btn.getAttribute("data-interval") === iv);
      btn.setAttribute("aria-pressed", btn.getAttribute("data-interval") === iv ? "true" : "false");
    });
    const badge = root.querySelector(".save-badge");
    if (badge) badge.classList.toggle("visible", iv === "year");

    root.querySelectorAll(".tier[data-plan]").forEach((card) => {
      const key = card.getAttribute("data-plan");
      const plan = plansByKey[key];
      if (!plan) return;
      const disp = (plan.display && plan.display[iv]) || {};
      const amount = card.querySelector(".price-amount");
      const period = card.querySelector(".price-period");
      if (amount) amount.textContent = disp.amount || "—";
      if (period) period.textContent = disp.period || "";

      const btn = card.querySelector(".upgrade-btn");
      if (btn && plan.price_ids) {
        const pid = plan.price_ids[iv];
        btn.dataset.priceId = pid || "";
        btn.dataset.interval = iv;
        btn.disabled = !pid;
      }
    });

    root.dataset.interval = iv;
    try { root.dispatchEvent(new CustomEvent("billing-interval", { detail: { interval: iv } })); } catch (_) {}
  }

  function initPricingToggle(root, plans) {
    if (!root) return;
    const plansByKey = {};
    (plans || []).forEach((p) => { plansByKey[p.key] = p; });
    root._plansByKey = plansByKey;

    root.querySelectorAll(".billing-toggle [data-interval]").forEach((btn) => {
      btn.addEventListener("click", () => {
        applyInterval(root, btn.getAttribute("data-interval"), plansByKey);
      });
    });

    applyInterval(root, root.dataset.interval || "month", plansByKey);
  }

  global.initPricingToggle = initPricingToggle;
  global.applyBillingInterval = applyInterval;
})(window);
