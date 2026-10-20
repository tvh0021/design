// Readout and keyboard movement for the activity map. The page is complete
// without this script: every cell carries its figures in aria-label.
(function () {
  "use strict";
  var map = document.getElementById("map");
  var out = document.getElementById("readout");
  if (!map || !out) return;
  var idle = out.getAttribute("data-idle") || "";

  function cellOf(el) {
    while (el && el !== map) {
      if (el.tagName === "TD" && el.hasAttribute("data-readout")) return el;
      el = el.parentNode;
    }
    return null;
  }
  function show(e) {
    var c = cellOf(e.target);
    if (c) out.textContent = c.getAttribute("data-readout");
  }
  function clear() {
    out.textContent = idle;
  }
  map.addEventListener("mouseover", show);
  map.addEventListener("focusin", show);
  map.addEventListener("mouseleave", clear);
  map.addEventListener("focusout", function (e) {
    if (!e.relatedTarget || !map.contains(e.relatedTarget)) clear();
  });

  // Arrow keys move between cells; Home and End jump within the row.
  map.addEventListener("keydown", function (e) {
    var c = cellOf(e.target);
    if (!c) return;
    var row = c.parentNode;
    var col = c.cellIndex;
    var next = null;
    switch (e.key) {
      case "ArrowLeft":
        next = row.cells[col - 1];
        break;
      case "ArrowRight":
        next = row.cells[col + 1];
        break;
      case "ArrowUp":
        next = row.previousElementSibling && row.previousElementSibling.cells[col];
        break;
      case "ArrowDown":
        next = row.nextElementSibling && row.nextElementSibling.cells[col];
        break;
      case "Home":
        next = row.cells[1];
        break;
      case "End":
        next = row.cells[row.cells.length - 1];
        break;
      default:
        return;
    }
    if (next && next.tagName === "TD") {
      e.preventDefault();
      next.focus();
    }
  });
})();
