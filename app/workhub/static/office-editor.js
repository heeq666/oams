/* Floor plan editor: drag to move, drag the round handle to resize.
   No build step, no dependencies -- same rule that kept htmx vendored.

   Geometry lives in data attributes on each <g class="item"> and is painted by
   rewriting the group's translate() and the rect's width/height, so a drag never
   has to touch the text nodes riding along inside the group.

   Every gesture ends in a POST to the same endpoint the old numeric form used,
   which means the server still validates geometry and still writes the audit
   event. The canvas is a view, never the source of truth. */
(function () {
  "use strict";

  var board = document.querySelector("[data-office-editor]");
  if (!board) return;
  var svg = board.querySelector("svg");
  if (!svg) return;
  var writable = !board.hasAttribute("data-readonly");

  var CW = Number(svg.getAttribute("data-w") || 1000);
  var CH = Number(svg.getAttribute("data-h") || 1000);
  var GRID = Number(svg.getAttribute("data-grid") || 20);
  var MIN = 60;

  var inspector = document.querySelector("[data-inspector]");
  var fields = inspector && inspector.querySelector("[data-fields]");
  var hint = inspector && inspector.querySelector("[data-hint]");
  var flash = document.querySelector("[data-editor-flash]");
  var labelInput = inspector && inspector.querySelector("[name=label]");
  var calibInput = inspector && inspector.querySelector("[name=calibrated]");
  var coordOut = inspector && inspector.querySelector("[data-coords]");
  var nameOut = inspector && inspector.querySelector("[data-name]");
  var roomLink = inspector && inspector.querySelector("[data-room-link]");
  var deleteForm = inspector && inspector.querySelector("form[data-delete]");

  var selected = null;
  var gesture = null;
  var pending = null;

  function snap(n) { return Math.round(n / GRID) * GRID; }
  function clamp(n, lo, hi) { return Math.max(lo, Math.min(n, hi)); }

  function geom(item) {
    return {
      x: Number(item.getAttribute("data-x")),
      y: Number(item.getAttribute("data-y")),
      w: Number(item.getAttribute("data-w")),
      h: Number(item.getAttribute("data-h"))
    };
  }

  function paint(item, g) {
    item.setAttribute("data-x", g.x);
    item.setAttribute("data-y", g.y);
    item.setAttribute("data-w", g.w);
    item.setAttribute("data-h", g.h);
    item.setAttribute("transform", "translate(" + g.x + " " + g.y + ")");
    item.querySelector("rect").setAttribute("width", g.w);
    item.querySelector("rect").setAttribute("height", g.h);
    var handle = item.querySelector("circle.handle");
    if (handle) {
      handle.setAttribute("cx", g.w);
      handle.setAttribute("cy", g.h);
    }
    item.setAttribute("aria-label",
      (item.getAttribute("data-name") || "区域") + "，" + g.x + "," + g.y + "，" + g.w + "×" + g.h);
    if (coordOut && item === selected) coordOut.textContent = g.x + "," + g.y + "  " + g.w + "×" + g.h;
  }

  function say(message, bad) {
    if (!flash) return;
    flash.textContent = message;
    flash.className = bad ? "flash error" : "flash";
    flash.hidden = false;
    clearTimeout(say._timer);
    say._timer = setTimeout(function () { flash.hidden = true; }, 2600);
  }

  function csrfValue() {
    // The editor posts from script, so the server-side form token has to travel
    // with it; without this the drag-to-save gesture would be rejected as forged.
    var holder = document.querySelector("[data-csrf]");
    return holder ? holder.getAttribute("data-csrf") : "";
  }

  function save(item) {
    if (!writable) return Promise.resolve();
    var body = new FormData();
    body.set("kind", item.getAttribute("data-kind"));
    body.set("org_unit_id", item.getAttribute("data-org"));
    body.set("position_id", item.getAttribute("data-position") || "");
    body.set("label", (labelInput && item === selected ? labelInput.value : item.getAttribute("data-label")) || "");
    body.set("x", item.getAttribute("data-x"));
    body.set("y", item.getAttribute("data-y"));
    body.set("w", item.getAttribute("data-w"));
    body.set("h", item.getAttribute("data-h"));
    body.set("calibrated", item.getAttribute("data-calibrated") === "true" ? "on" : "");
    body.set("csrf", csrfValue());
    return fetch("/admin/spaces", { method: "POST", body: body, redirect: "follow" })
      .then(function (r) { say(r.ok ? "已保存" : "保存失败，请重试", !r.ok); })
      .catch(function () { say("保存失败，网络不通", true); });
  }

  function select(item) {
    if (selected) selected.classList.remove("selected");
    selected = item;
    if (fields) fields.hidden = !item;
    if (hint) hint.hidden = !!item;
    if (!item) return;
    item.classList.add("selected");
    if (!inspector) return;
    if (nameOut) nameOut.textContent = item.getAttribute("data-name") || "";
    if (labelInput) labelInput.value = item.getAttribute("data-label") || "";
    if (calibInput) calibInput.checked = item.getAttribute("data-calibrated") === "true";
    if (coordOut) {
      var g = geom(item);
      coordOut.textContent = g.x + "," + g.y + "  " + g.w + "×" + g.h;
    }
    if (roomLink) {
      var room = item.getAttribute("data-room");
      if (room) { roomLink.href = room; roomLink.hidden = false; } else { roomLink.hidden = true; }
    }
    if (deleteForm) deleteForm.action = "/admin/spaces/" + item.getAttribute("data-space") + "/remove";
  }

  function pointer(evt) {
    var pt = svg.createSVGPoint();
    pt.x = evt.clientX;
    pt.y = evt.clientY;
    var m = svg.getScreenCTM();
    if (!m) return { x: 0, y: 0 };
    var p = pt.matrixTransform(m.inverse());
    return { x: p.x, y: p.y };
  }

  svg.addEventListener("pointerdown", function (evt) {
    var handle = evt.target.closest && evt.target.closest("circle.handle");
    var item = evt.target.closest && evt.target.closest("g.item");
    if (!item) { select(null); return; }
    select(item);
    if (!writable) return;
    gesture = { mode: handle ? "resize" : "move", item: item, start: pointer(evt), from: geom(item) };
    svg.setPointerCapture(evt.pointerId);
    evt.preventDefault();
  });

  svg.addEventListener("pointermove", function (evt) {
    if (!gesture) return;
    var p = pointer(evt);
    var dx = p.x - gesture.start.x;
    var dy = p.y - gesture.start.y;
    var o = gesture.from;
    if (gesture.mode === "move") {
      paint(gesture.item, {
        x: clamp(snap(o.x + dx), 0, CW - o.w),
        y: clamp(snap(o.y + dy), 0, CH - o.h),
        w: o.w, h: o.h
      });
    } else {
      paint(gesture.item, {
        x: o.x, y: o.y,
        w: clamp(snap(o.w + dx), MIN, CW - o.x),
        h: clamp(snap(o.h + dy), MIN, CH - o.y)
      });
    }
  });

  function endGesture(evt) {
    if (!gesture) return;
    var item = gesture.item;
    gesture = null;
    if (svg.hasPointerCapture && evt.pointerId !== undefined) {
      try { svg.releasePointerCapture(evt.pointerId); } catch (e) { /* already gone */ }
    }
    save(item);
  }
  svg.addEventListener("pointerup", endGesture);
  svg.addEventListener("pointercancel", endGesture);

  /* Keyboard: tab to a desk or zone, arrows nudge, shift for a bigger step.
     Dragging is not the only way in. */
  Array.prototype.forEach.call(svg.querySelectorAll("g.item"), function (item) {
    item.addEventListener("keydown", function (evt) {
      var step = evt.shiftKey ? GRID * 5 : GRID;
      var g = geom(item);
      var moved = null;
      if (evt.key === "ArrowLeft") moved = { x: g.x - step, y: g.y };
      else if (evt.key === "ArrowRight") moved = { x: g.x + step, y: g.y };
      else if (evt.key === "ArrowUp") moved = { x: g.x, y: g.y - step };
      else if (evt.key === "ArrowDown") moved = { x: g.x, y: g.y + step };
      else if (evt.key === "Enter" || evt.key === " ") { select(item); evt.preventDefault(); return; }
      else return;
      evt.preventDefault();
      if (!writable) return;
      paint(item, { x: clamp(moved.x, 0, CW - g.w), y: clamp(moved.y, 0, CH - g.h), w: g.w, h: g.h });
      clearTimeout(pending);
      pending = setTimeout(function () { save(item); }, 400);
    });
    item.addEventListener("focus", function () { select(item); });
  });

  if (labelInput) {
    labelInput.addEventListener("change", function () {
      if (!selected) return;
      selected.setAttribute("data-label", labelInput.value);
      save(selected);
    });
  }
  if (calibInput) {
    calibInput.addEventListener("change", function () {
      if (!selected) return;
      selected.setAttribute("data-calibrated", calibInput.checked ? "true" : "false");
      var rect = selected.querySelector("rect");
      if (calibInput.checked) rect.removeAttribute("stroke-dasharray");
      else rect.setAttribute("stroke-dasharray", "18 12");
      selected.classList.toggle("uncalibrated", !calibInput.checked);
      save(selected);
    });
  }

  /* Adding: drop the new rectangle into the first slot on the grid that nothing
     else occupies, so it never lands on top of an existing one. */
  function freeSpot(w, h) {
    var taken = Array.prototype.map.call(svg.querySelectorAll("g.item"), geom);
    for (var y = 0; y + h <= CH; y += GRID) {
      for (var x = 0; x + w <= CW; x += GRID) {
        var clash = taken.some(function (t) {
          return x < t.x + t.w && x + w > t.x && y < t.y + t.h && y + h > t.y;
        });
        if (!clash) return { x: x, y: y };
      }
    }
    return { x: 0, y: 0 };
  }

  Array.prototype.forEach.call(board.querySelectorAll("[data-add]"), function (button) {
    button.addEventListener("click", function () {
      var form = button.closest("form");
      var w = Number(button.getAttribute("data-add-w") || 320);
      var h = Number(button.getAttribute("data-add-h") || 220);
      var spot = freeSpot(w, h);
      form.querySelector("[name=x]").value = spot.x;
      form.querySelector("[name=y]").value = spot.y;
      form.querySelector("[name=w]").value = w;
      form.querySelector("[name=h]").value = h;
      form.submit();
    });
  });
})();
