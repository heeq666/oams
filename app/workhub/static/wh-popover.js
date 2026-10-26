/* 浮层载体：菜单、下拉框展开层、将来的弹窗，全走这一套（design-system-v1 §7.3）。
 *
 * 为什么要有这个文件：玻璃能不能真起来，不取决于白多少，取决于面板挂在 DOM 的
 * 哪一层。<details>、带 transform / filter / opacity<1 / mask 的容器都是 backdrop
 * root，会把 backdrop-filter 的采样范围关在自己里面，玻璃就退化成一层平白，底下的
 * 字照样清晰可读——这正是账号菜单一度"比顶栏胶囊透得多"的原因。所以这里把两件事
 * 定死：面板永远挂 <body> + position: fixed，位置由本模块算。
 *
 * 声明式用法（不需要写 JS）：
 *
 *   <button data-wh-popover aria-controls="account-menu" aria-expanded="false">…</button>
 *   <div class="wh-popover" id="account-menu" hidden>…</div>
 *
 * 可选属性：data-wh-align="end|start"（默认 end，end = 面板右沿贴触发器右沿）、
 *          data-wh-side="below|above|auto"（默认 below；auto 空间不够就翻上）
 *          data-wh-gap="10"、data-wh-min-width="208"
 *
 * 脚本式用法（面板是运行时造的，比如下拉框展开层）：
 *
 *   var inst = whPopover.attach({ trigger: select, panel: list, align: 'start',
 *                                 side: 'auto', gap: 6, minWidth: 168,
 *                                 matchTrigger: true, manual: true,
 *                                 onClose: cleanup });
 *   inst.open(); inst.close(); inst.place(); inst.isOpen();
 *
 * 同屏只有一个浮层：开新的先关旧的。关闭有三条路，缺一条就会把面板留在页面上：
 * 点到别处（mousedown）、Esc（焦点还给触发器）、焦点移出（Tab 走）。没有遮罩，
 * 所以这三条必须由脚本兜住。
 */
(function () {
  'use strict';

  var MARGIN = 8;          // 面板与视口边缘的最小距离
  var current = null;      // 唯一展开的实例
  var listening = false;
  var scheduled = false;

  function num(value, fallback) {
    var n = parseFloat(value);
    return isFinite(n) ? n : fallback;
  }

  function clamp(value, low, high) {
    return Math.min(Math.max(value, low), high);
  }

  // 摆放：横向按 align 贴住触发器，纵向按 side 挂在下方或上方，四个边都夹进
  // 视口。面板比视口高时收成可滚动的 max-height，免得顶穿屏幕。
  function place(inst) {
    var panel = inst.panel;
    if (!panel || panel.hidden) return;
    var trigger = inst.trigger;
    var rect = trigger.getBoundingClientRect();
    var maxHeight = window.innerHeight - MARGIN * 2;

    panel.style.maxHeight = '';
    if (panel.offsetHeight > maxHeight) panel.style.maxHeight = maxHeight + 'px';

    var width = inst.matchTrigger
      ? clamp(Math.max(rect.width, inst.minWidth), 0, window.innerWidth - MARGIN * 2)
      : Math.min(Math.max(panel.offsetWidth, inst.minWidth), window.innerWidth - MARGIN * 2);
    if (inst.matchTrigger) panel.style.width = Math.round(width) + 'px';

    var height = panel.offsetHeight;
    var left = inst.align === 'start' ? rect.left : rect.right - width;
    left = clamp(left, MARGIN, Math.max(MARGIN, window.innerWidth - width - MARGIN));

    var below = rect.bottom + inst.gap;
    var above = rect.top - height - inst.gap;
    var side = inst.side;
    if (side === 'auto') {
      var roomBelow = window.innerHeight - below - MARGIN;
      var roomAbove = rect.top - inst.gap - MARGIN;
      // 下方放不下一屏能看的行数（140），而上方更宽裕时才翻上去。
      side = (roomBelow < Math.min(height, 140) && roomAbove > roomBelow) ? 'above' : 'below';
    }
    var top = clamp(side === 'above' ? above : below,
                    MARGIN, Math.max(MARGIN, window.innerHeight - height - MARGIN));

    panel.style.left = Math.round(left) + 'px';
    panel.style.top = Math.round(top) + 'px';
    panel.setAttribute('data-side', side);
    panel.setAttribute('data-align', inst.align);
  }

  function reopenPosition() {
    if (!current) return;
    // 触发器滚出视口就别让面板悬在空处。
    var rect = current.trigger.getBoundingClientRect();
    if (rect.bottom < 0 || rect.top > window.innerHeight) { close(current, false); return; }
    place(current);
  }

  function schedulePosition() {
    if (scheduled) return;
    scheduled = true;
    window.requestAnimationFrame(function () {
      scheduled = false;
      reopenPosition();
    });
  }

  function onPointerDown(event) {
    if (!current) return;
    var path = event.composedPath ? event.composedPath() : [];
    if (path.indexOf(current.panel) >= 0 || path.indexOf(current.trigger) >= 0) return;
    close(current, false);   // 先收面板，这次点击照常落到页面上
  }

  function onKeydown(event) {
    if (!current) return;
    if (event.key === 'Escape') {
      event.preventDefault();
      var target = current;
      close(target, true);
    }
  }

  function onFocusOut(event) {
    if (!current) return;
    var to = event.relatedTarget;
    if (!to) return;                                  // 焦点离开浏览器，别抢着关
    if (current.panel.contains(to)) return;
    if (current.trigger.contains(to)) return;
    close(current, false);
  }

  function onScroll(event) {
    if (!current) return;
    if (current.panel.contains(event.target)) return;  // 面板自己滚动不算
    schedulePosition();
  }

  function onResize() {
    if (current) place(current);
  }

  function listen(active) {
    if (active === listening) return;
    listening = active;
    var method = active ? 'addEventListener' : 'removeEventListener';
    document[method]('mousedown', onPointerDown, true);
    document[method]('keydown', onKeydown, true);
    document[method]('focusout', onFocusOut, true);
    document[method]('scroll', onScroll, true);
    window[method]('resize', onResize);
  }

  function open(inst) {
    if (!inst || !inst.panel) return inst;
    if (current && current !== inst) close(current, false);
    current = inst;
    inst.panel.hidden = false;
    inst.trigger.setAttribute('aria-expanded', 'true');
    place(inst);
    listen(true);
    if (inst.onOpen) inst.onOpen();
    return inst;
  }

  function close(inst, restoreFocus) {
    if (!inst || !inst.panel || inst.panel.hidden) return;
    inst.panel.hidden = true;
    inst.trigger.setAttribute('aria-expanded', 'false');
    if (current === inst) current = null;
    if (!current) listen(false);
    if (inst.onClose) inst.onClose();
    if (restoreFocus && inst.trigger.focus) inst.trigger.focus({ preventScroll: true });
  }

  function closeAll() {
    if (current) close(current, false);
  }

  // 把一只触发器和一个面板接起来。manual = 开关由调用方管（下拉框自己处理点开），
  // 否则这里绑 click 做开合。
  function attach(options) {
    var trigger = options.trigger;
    var panel = options.panel;
    if (!trigger || !panel) return null;
    if (!panel.classList.contains('wh-popover')) panel.classList.add('wh-popover');
    // 面板必须直挂 body：任何祖先都可能成为 backdrop root（见文件头）。
    if (panel.parentNode !== document.body) document.body.appendChild(panel);
    panel.hidden = true;

    var inst = {
      trigger: trigger,
      panel: panel,
      align: options.align || panel.getAttribute('data-wh-align') || 'end',
      side: options.side || panel.getAttribute('data-wh-side') || 'below',
      gap: options.gap != null ? options.gap : num(panel.getAttribute('data-wh-gap'), 10),
      minWidth: options.minWidth != null ? options.minWidth : num(panel.getAttribute('data-wh-min-width'), 0),
      matchTrigger: !!options.matchTrigger,
      manual: !!options.manual,
      onOpen: options.onOpen || null,
      onClose: options.onClose || null,
      open: function () { return open(inst); },
      close: function (restoreFocus) { close(inst, restoreFocus !== false); },
      place: function () { place(inst); },
      isOpen: function () { return !panel.hidden; }
    };

    if (!inst.manual) {
      trigger.addEventListener('click', function (event) {
        event.preventDefault();
        if (panel.hidden) inst.open(); else close(inst, false);
      });
    }
    trigger.setAttribute('aria-expanded', 'false');
    return inst;
  }

  // 扫描声明式的浮层。htmx 换块之后也要再扫一遍，否则新插进来的下拉框没人管。
  function init(root) {
    var nodes = (root || document).querySelectorAll('[data-wh-popover]');
    for (var i = 0; i < nodes.length; i++) {
      var trigger = nodes[i];
      if (trigger.__whPopover) continue;
      var id = trigger.getAttribute('aria-controls');
      var panel = id ? document.getElementById(id) : null;
      if (!panel) continue;
      trigger.__whPopover = attach({ trigger: trigger, panel: panel });
    }
  }

  document.addEventListener('DOMContentLoaded', function () { init(document); });
  document.addEventListener('htmx:afterSwap', function () { init(document); });
  // htmx 换块之前先收面板，否则它会在旧节点上悬着。
  document.addEventListener('htmx:beforeSwap', closeAll);

  // 对外接口。open/close 既能接实例，也能直接接触发器元素——将来弹窗从按钮里
  // 用 onclick 打开时，不该逼调用方先记住实例。
  function instance(target) {
    if (!target) return null;
    if (target.panel && target.trigger) return target;
    return target.__whPopover || null;
  }

  window.whPopover = {
    attach: attach,
    init: init,
    open: function (target) { var inst = instance(target); return inst ? open(inst) : null; },
    close: function (target, restoreFocus) {
      var inst = instance(target);
      if (inst) close(inst, restoreFocus === true);
    },
    closeAll: closeAll,
    current: function () { return current; }
  };
})();
