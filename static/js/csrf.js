/**
 * MyLibrary security glue:
 *  1. Injects the per-session CSRF token into every <form> before submission
 *     (server rejects state-changing requests without it).
 *  2. Handles [data-confirm] buttons — replaces the need for inline
 *     onsubmit handlers, which a strict Content-Security-Policy forbids.
 */
(function () {
  "use strict";

  var meta = document.querySelector('meta[name="csrf-token"]');
  var token = meta ? meta.getAttribute("content") : "";
  if (!token) return;

  // A request may race ahead of DOMContentLoaded growing old forms in; capture
  // at the document level so every form is covered.
  document.addEventListener("submit", function (event) {
    var form = event.target;
    if (form.method && form.method.toLowerCase() === "post") {
      if (!form.querySelector('input[name="csrf_token"]')) {
        var input = document.createElement("input");
        input.type = "hidden";
        input.name = "csrf_token";
        input.value = token;
        form.appendChild(input);
      }
      if (form.hasAttribute("data-confirm")) {
        if (!window.confirm(form.getAttribute("data-confirm"))) {
          event.preventDefault();
          event.stopPropagation();
          return false;
        }
      }
    }
  }, true); // capture: runs before native form submission

  // Remove cover images that fail to load (spine fallback renders instead).
  document.addEventListener("error", function (event) {
    var img = event.target;
    if (img && img.tagName === "IMG" && img.hasAttribute("data-fallback-remove")) {
      img.remove();
    }
  }, true); // 'error' does not bubble — capture required

  // Flash dismissal.
  document.addEventListener("click", function (event) {
    var btn = event.target.closest("[data-dismiss-flash]");
    if (btn) btn.parentElement.remove();
  });
})();
