/* CSRF token plumbing for fetch / htmx / XHR.
 *
 * Pairs with app/web/middleware/csrf.py. The middleware publishes a readable
 * cookie `persona_csrf` whose value is HMAC(session_token, "persona-csrf-v1").
 * Everything here does is copy that value into the `X-CSRF-Token` header of
 * every same-origin state-changing request, so no call site has to care.
 *
 * Load it from base.html BEFORE htmx/alpine:
 *     <script src="/static/csrf.js?v={{ app_version }}"></script>
 *
 * Safe to load unconditionally: with no session cookie the token is empty and
 * the header is simply not set.
 */
(function () {
  "use strict";

  var COOKIE = "persona_csrf";
  var HEADER = "X-CSRF-Token";
  var UNSAFE = { POST: 1, PUT: 1, PATCH: 1, DELETE: 1 };

  function token() {
    var m = document.cookie.match(/(?:^|;\s*)persona_csrf=([^;]*)/);
    return m ? decodeURIComponent(m[1]) : "";
  }

  /* Same-origin only. Never leak the token to a third party: an absolute URL
   * to another origin must go out without the header. */
  function sameOrigin(url) {
    try {
      return new URL(url, window.location.href).origin === window.location.origin;
    } catch (e) {
      return false;
    }
  }

  /* --- fetch ------------------------------------------------------------ */
  if (window.fetch) {
    var nativeFetch = window.fetch.bind(window);
    window.fetch = function (input, init) {
      init = init || {};
      var url = typeof input === "string" ? input : (input && input.url) || "";
      var method = String(
        init.method || (input && input.method) || "GET"
      ).toUpperCase();
      var value = token();
      if (value && UNSAFE[method] && sameOrigin(url)) {
        var headers = new Headers(init.headers || (input && input.headers) || {});
        if (!headers.has(HEADER)) headers.set(HEADER, value);
        init = Object.assign({}, init, { headers: headers });
      }
      return nativeFetch(input, init);
    };
  }

  /* --- XMLHttpRequest --------------------------------------------------- */
  if (window.XMLHttpRequest) {
    var open = XMLHttpRequest.prototype.open;
    var sendXhr = XMLHttpRequest.prototype.send;
    XMLHttpRequest.prototype.open = function (method, url) {
      this.__personaCsrf =
        UNSAFE[String(method || "GET").toUpperCase()] && sameOrigin(url);
      return open.apply(this, arguments);
    };
    XMLHttpRequest.prototype.send = function () {
      var value = token();
      if (this.__personaCsrf && value) {
        try {
          this.setRequestHeader(HEADER, value);
        } catch (e) {
          /* header already set, or the request is in a bad state — ignore */
        }
      }
      return sendXhr.apply(this, arguments);
    };
  }

  /* --- htmx ------------------------------------------------------------- */
  document.addEventListener("htmx:configRequest", function (evt) {
    var value = token();
    if (!value) return;
    var verb = String((evt.detail && evt.detail.verb) || "get").toUpperCase();
    if (!UNSAFE[verb]) return;
    evt.detail.headers[HEADER] = value;
  });

  /* --- plain <form method="post"> --------------------------------------- */
  /* Forms that already carry a hidden csrf_token field are left alone; the
   * rest get one injected at submit time. This is a belt-and-braces path so a
   * template the rollout missed still works once enforcement is on.
   *
   * CAVEAT worth knowing before you rely on this: a *programmatic*
   * `form.submit()` does NOT fire a submit event, so nothing here runs for it.
   * Any form submitted from script (`onchange="this.form.submit()"`, a form
   * built in JS) must carry `{{ csrf_input(request) }}` in the markup. */
  document.addEventListener(
    "submit",
    function (evt) {
      var form = evt.target;
      if (!form || form.tagName !== "FORM") return;
      var method = String(form.getAttribute("method") || "GET").toUpperCase();
      if (!UNSAFE[method]) return;
      var action = form.getAttribute("action") || window.location.href;
      if (!sameOrigin(action)) return;
      var value = token();
      if (!value) return;

      /* multipart/form-data: the middleware deliberately never buffers a file
       * upload to look for the field (that would be a memory-exhaustion bug),
       * so a hidden input is INVISIBLE to it. The documented escape hatch is
       * the query parameter — rewrite the action instead. */
      var enctype = String(
        form.getAttribute("enctype") || form.enctype || ""
      ).toLowerCase();
      if (enctype.indexOf("multipart/form-data") === 0) {
        try {
          var url = new URL(action, window.location.href);
          if (!url.searchParams.get("csrf_token")) {
            url.searchParams.set("csrf_token", value);
            form.setAttribute("action", url.pathname + url.search + url.hash);
          }
        } catch (e) {
          /* unparseable action — leave it alone rather than corrupt it */
        }
        return;
      }

      if (form.querySelector('input[name="csrf_token"]')) return;
      var input = document.createElement("input");
      input.type = "hidden";
      input.name = "csrf_token";
      input.value = value;
      form.appendChild(input);
    },
    true
  );
})();
