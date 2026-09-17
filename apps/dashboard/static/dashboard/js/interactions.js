/* RamAI — Interactions */
(function () {
  'use strict';

  // Countdown to commitment deadline
  function initDeadlineCountdown() {
    var el = document.getElementById('deadline-countdown');
    if (!el) return;
    var deadlineStr = el.dataset.deadline;
    if (!deadlineStr) return;
    var deadline = new Date(deadlineStr);
    function update() {
      var diff = deadline - new Date();
      if (diff <= 0) { el.textContent = 'Waktu habis!'; return; }
      var h = Math.floor(diff / 3600000);
      var m = Math.floor((diff % 3600000) / 60000);
      var s = Math.floor((diff % 60000) / 1000);
      el.textContent = h + 'j ' + m + 'm ' + s + 'd lagi';
    }
    update();
    setInterval(update, 1000);
  }

  // Collapsible
  function initCollapsible() {
    document.querySelectorAll('[data-toggle-collapse]').forEach(function (toggle) {
      var targetId = toggle.getAttribute('data-toggle-collapse');
      var target = document.getElementById(targetId);
      if (!target) return;
      // Init collapsed
      target.style.maxHeight = '0';
      target.style.opacity = '0';
      target.style.overflow = 'hidden';
      target.style.transition = 'all .3s ease';
      toggle.setAttribute('aria-expanded', 'false');

      toggle.addEventListener('click', function () {
        var expanded = this.getAttribute('aria-expanded') === 'true';
        this.setAttribute('aria-expanded', !expanded);
        if (expanded) {
          target.style.maxHeight = '0';
          target.style.opacity = '0';
        } else {
          target.style.maxHeight = target.scrollHeight + 60 + 'px';
          target.style.opacity = '1';
        }
      });
    });
  }

  // HTMX handlers
  function initHTMX() {
    document.addEventListener('htmx:afterSettle', function () {
      initDeadlineCountdown();
      initCollapsible();
    });
  }

  function init() {
    initDeadlineCountdown();
    initCollapsible();
    initHTMX();
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }
})();
