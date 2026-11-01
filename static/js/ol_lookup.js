/**
 * MyLibrary × Open Library
 *  1. ISBN field: on change/fetch-button, pull title/author/publisher/year/
 *     description/genres/cover from Open Library and fill the form.
 *  2. No-ISBN discovery: query Open Library by title/author and offer the
 *     top results as fillable cards.
 * Everything is set via .value/textContent (never innerHTML) — CSP-friendly.
 */
(function () {
  "use strict";

  var form = document.querySelector("form.submit-form");
  if (!form) return;

  var isbnInput = document.getElementById("isbn-input");
  var fetchBtn = document.getElementById("isbn-fetch");
  var statusEl = document.getElementById("lookup-status");
  var queryInput = document.getElementById("ol-query");
  var searchBtn = document.getElementById("ol-search");
  var resultsEl = document.getElementById("ol-results");

  var ISBN_RE = /^(\d{9}[\dX]|\d{13})$/i;

  function setStatus(kind, message) {
    if (!statusEl) return;
    statusEl.className = "lookup-status " + (kind || "");
    statusEl.textContent = message || "";
  }

  // Dominant errors we show in one place; "not_found/unreachable" speak for themselves.
  var ERRORS = {
    invalid_isbn: "That doesn't look like a valid ISBN-10 or ISBN-13.",
    not_found: "Open Library has no record for that ISBN.",
    openlibrary_unreachable: "Couldn't reach Open Library just now — try again shortly."
  };

  function getJSON(url, onNext) {
    fetch(url, { credentials: "same-origin" })
      .then(function (res) {
        return res.json().then(function (data) {
          return { ok: res.ok, status: res.status, data: data };
        });
      })
      .then(function (out) { onNext(out); })
      .catch(function () { onNext({ ok: false, status: 502, data: { error: "openlibrary_unreachable" } }); });
  }

  function fillText(name, value) {
    var el = form.querySelector('[name="' + name + '"]');
    if (el && value != null) el.value = value;
  }

  function fillGenres(names) {
    var el = form.querySelector('[name="genres"]');
    if (el && names && names.length) {
      var existing = el.value.split(",").map(function (s) { return s.trim(); }).filter(Boolean).map(dbg);
      function dbg(s) { return s.toLowerCase(); }
      var merged = existing;
      names.forEach(function (g) {
        if (merged.indexOf(g.toLowerCase()) === -1) merged.push(g);
      });
      el.value = merged.join(", ");
    }
  }

  function fillForm(data) {
    if (data.isbn) fillText("isbn", data.isbn);
    if (data.title) fillText("title", data.title);
    if (data.author) fillText("author", data.author);
    if (data.publisher) fillText("publisher", data.publisher);
    if (data.published_year) fillText("published_year", String(data.published_year));
    if (data.cover_url) fillText("cover_url", data.cover_url);
    if (data.description) fillText("description", data.description);
    fillGenres(data.genres || []);
  }

  function showDuplicate(duplicate) {
    if (!statusEl || !duplicate) return;
    statusEl.className = "lookup-status duplicate";
    var a = document.createElement("a");
    a.href = "/book/" + duplicate.id;
    a.textContent = "Already in the catalogue (opens its page after you send your shelf).";
    statusEl.textContent = "Already in the catalogue — ";
    statusEl.appendChild(a);
  }

  function lookupISBN(isbnRaw) {
    var isbn = (isbnRaw || "").replace(/-|\s/g, "").toUpperCase();
    if (!ISBN_RE.test(isbn)) {
      setStatus("error", ERRORS.invalid_isbn);
      return;
    }
    isbnInput.value = isbn;
    setStatus("busy", "Fetching from Open Library…");
    getJSON("/api/books/lookup?isbn=" + encodeURIComponent(isbn), function (out) {
      if (!out.ok) {
        setStatus("error", ERRORS[out.data && out.data.error] || "Lookup failed.");
        return;
      }
      fillForm(out.data);
      if (out.data.duplicate) {
        showDuplicate(out.data.duplicate);
      } else {
        setStatus("ok", "Details fetched — review, then submit for approval.");
      }
    });
  }

  if (fetchBtn) fetchBtn.addEventListener("click", function () { lookupISBN(isbnInput.value); });
  if (isbnInput) {
    isbnInput.addEventListener("change", function () {
      if (ISBN_RE.test(isbnInput.value.replace(/-|\s/g, "").toUpperCase())) {
        lookupISBN(isbnInput.value);
      }
    });
  }

  // ----- keyword discovery (no ISBN) -------------------------------------
  function makeResultCard(data) {
    var card = document.createElement("button");
    card.type = "button";
    card.className = "ol-result";

    if (data.cover_url) {
      var img = document.createElement("img");
      img.src = "/cover-preview?url=" + encodeURIComponent(data.cover_url);
      img.alt = "";
      img.loading = "lazy";
      card.appendChild(img);
    }

    var text = document.createElement("span");
    text.className = "ol-result-text";
    var title = document.createElement("strong");
    title.textContent = data.title || "(untitled)";
    text.appendChild(title);
    var meta = document.createElement("small");
    var bits = [];
    if (data.author) bits.push(data.author);
    if (data.published_year) bits.push(data.published_year);
    meta.textContent = bits.join(" · ");
    text.appendChild(meta);
    card.appendChild(text);

    card.addEventListener("click", function () {
      fillForm(data);
      setStatus("ok", data.title + " — filled in. Review, then submit.");
      resultsEl.hidden = true;
      window.scrollTo({ top: form.offsetTop - 40, behavior: "smooth" });
    });
    return card;
  }

  if (searchBtn) {
    searchBtn.addEventListener("click", function () {
      var q = (queryInput.value || "").trim();
      if (!q) return;
      resultsEl.textContent = "";
      var busy = document.createElement("p");
      busy.className = "ol-busy";
      busy.textContent = "Searching Open Library…";
      resultsEl.appendChild(busy);
      resultsEl.hidden = false;
      getJSON("/api/books/search?q=" + encodeURIComponent(q), function (out) {
        resultsEl.textContent = "";
        if (!out.ok) {
          var err = document.createElement("p");
          err.className = "ol-error";
          err.textContent = ERRORS[out.data && out.data.error] || "Search failed.";
          resultsEl.appendChild(err);
          return;
        }
        var results = out.data.results || [];
        if (!results.length) {
          var none = document.createElement("p");
          none.className = "ol-error";
          none.textContent = "No Open Library hits for that search.";
          resultsEl.appendChild(none);
          return;
        }
        var note = document.createElement("p");
        note.className = "ol-note";
        note.textContent = "Tap a result to fill the form — adjust anything before submitting.";
        resultsEl.appendChild(note);
        results.forEach(function (data) {
          resultsEl.appendChild(makeResultCard(data));
        });
      });
    });
    queryInput.addEventListener("keydown", function (event) {
      if (event.key === "Enter") {
        event.preventDefault();
        searchBtn.click();
      }
    });
  }
})();
