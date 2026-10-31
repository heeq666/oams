/* 下拉框：把原生 <select> 的展开层换成我们自己的玻璃面板（design-system-v1 §7.3）。
 *
 * 为什么还留着原生 <select>：它才是表单的主人。值是它提交的、name/required/
 * disabled 是它说了算、没有 JS 时它还照常能用；我们只接管「点开」这一步，把系统
 * 画的弹出层换成和菜单同一套玻璃。
 *
 * 摆放、层级、点外部/Esc 关闭全归 static/wh-popover.js（浮层载体），这里只管
 * select 自己的事：选项行的内容、键盘导航、typeahead、把值写回表单。以前这些
 * 「面板该长在哪」的逻辑在本文件和 base.html 里各写了一遍，两处都漏过同一类坑。
 *
 * 用事件委托而不是初始化扫描：htmx 会把表格整块换掉，页面里也会运行
 * 时造出新的 <select>，逐个绑定必然漏。点在谁身上就给谁开。
 */
(function () {
  'use strict';

  var inst = null;        // 当前展开的浮层实例（wh-popover 的）
  var owner = null;       // 面板属于哪个 <select>
  var rows = [];          // [{ el, value, disabled }]
  var active = -1;        // 键盘高亮行
  var typeahead = '';
  var typeaheadAt = 0;
  var closing = false;
  var seq = 0;

  // 触屏保留系统选择器：iOS / Android 的滚轮比一块 320 高的浮层好用，而且手指
  // 点按会合成 mousedown，不排除就会把系统弹层顶掉。只增强鼠标环境。
  function coarsePointer() {
    return !!(window.matchMedia && window.matchMedia('(pointer: coarse)').matches);
  }

  function isEnhanceable(el) {
    return el && el.tagName === 'SELECT' && !el.multiple && el.size <= 1 && !el.disabled && !coarsePointer();
  }

  function optionsOf(select) {
    var out = [];
    for (var i = 0; i < select.options.length; i++) {
      var opt = select.options[i];
      out.push({
        value: opt.value,
        label: (opt.label || opt.textContent || '').trim(),
        disabled: opt.disabled,
        selected: opt.selected,
        group: opt.parentNode && opt.parentNode.tagName === 'OPTGROUP' ? opt.parentNode.label : ''
      });
    }
    return out;
  }

  // 面板关掉之后的收尾。由载体调用：它管开合，这里管自己的 DOM 与 aria。
  function teardown() {
    document.removeEventListener('keydown', onKeydown, true);
    if (owner) {
      owner.classList.remove('is-open');
      owner.removeAttribute('aria-controls');
      owner.removeAttribute('aria-activedescendant');
    }
    var panel = inst && inst.panel;
    if (panel && panel.parentNode) panel.parentNode.removeChild(panel);
    inst = null; owner = null; rows = []; active = -1;
  }

  function close(restoreFocus) {
    if (!inst || closing) return;
    var select = owner;
    closing = true;
    inst.close(false);          // 载体会回调 teardown
    closing = false;
    if (restoreFocus !== false && select) select.focus({ preventScroll: true });
  }

  function setActive(index) {
    if (!rows.length) return;
    var step = index < active ? -1 : 1;
    var next = index;
    while (rows[next] && rows[next].disabled) next += step;
    if (!rows[next]) return;
    if (rows[active]) rows[active].classList.remove('is-active');
    active = next;
    rows[active].classList.add('is-active');
    rows[active].scrollIntoView({ block: 'nearest' });
    if (owner && rows[active].id) owner.setAttribute('aria-activedescendant', rows[active].id);
    if (owner) owner.focus({ preventScroll: true });
  }

  function commit(index) {
    var row = rows[index];
    if (!row || row.disabled) return;
    var select = owner;
    select.value = row.dataset.value;
    select.dispatchEvent(new Event('input', { bubbles: true }));
    select.dispatchEvent(new Event('change', { bubbles: true }));
    close(false);
    select.focus({ preventScroll: true });
  }

  function onKeydown(event) {
    if (!inst) return;
    var key = event.key;
    // Esc 归载体管（它还要把焦点还给触发器），这里不抢。
    if (key === 'Tab') { close(false); return; }
    if (key === 'ArrowDown') { event.preventDefault(); setActive(Math.min(active + 1, rows.length - 1)); return; }
    if (key === 'ArrowUp') { event.preventDefault(); setActive(Math.max(active - 1, 0)); return; }
    if (key === 'Home') { event.preventDefault(); setActive(0); return; }
    if (key === 'End') { event.preventDefault(); setActive(rows.length - 1); return; }
    if (key === 'Enter' || key === ' ') { event.preventDefault(); if (active >= 0) commit(active); return; }
    if (key.length === 1) {
      var now = Date.now();
      typeahead = (now - typeaheadAt > 600 ? '' : typeahead) + key.toLowerCase();
      typeaheadAt = now;
      for (var i = 0; i < rows.length; i++) {
        var label = rows[i].textContent.trim().toLowerCase();
        if (!rows[i].disabled && label.indexOf(typeahead) === 0) { setActive(i); break; }
      }
    }
  }

  function open(select) {
    close(false);
    var options = optionsOf(select);
    if (!options.length) return;

    var list = document.createElement('div');
    list.className = 'wh-list';
    list.id = 'wh-list-' + (++seq);
    list.setAttribute('role', 'listbox');
    list.setAttribute('aria-label', select.getAttribute('aria-label') || select.name || '选项');
    rows = [];
    active = -1;
    var lastGroup = null;
    for (var i = 0; i < options.length; i++) {
      var opt = options[i];
      if (opt.group && opt.group !== lastGroup) {
        var head = document.createElement('div');
        head.className = 'wh-group';
        head.textContent = opt.group;
        list.appendChild(head);
      }
      lastGroup = opt.group || null;
      var row = document.createElement('div');
      row.className = 'wh-opt';
      row.id = list.id + '-opt-' + i;
      row.setAttribute('role', 'option');
      row.dataset.value = opt.value;
      row.setAttribute('aria-selected', opt.selected ? 'true' : 'false');
      if (opt.disabled) row.setAttribute('aria-disabled', 'true');
      var tick = document.createElement('span');
      tick.className = 'tick';
      tick.setAttribute('aria-hidden', 'true');
      tick.textContent = opt.selected ? '✓' : '';
      row.appendChild(tick);
      row.appendChild(document.createTextNode(opt.label || '\u00a0'));
      list.appendChild(row);
      rows.push(row);
      if (opt.selected) active = i;
    }

    owner = select;
    inst = window.whPopover.attach({
      trigger: select,
      panel: list,
      align: 'start',
      side: 'auto',
      gap: 6,
      minWidth: 168,
      matchTrigger: true,
      manual: true,          // 开关由下面的委托管
      onClose: teardown
    });
    if (!inst) return;
    inst.open();
    select.classList.add('is-open');
    select.setAttribute('aria-controls', list.id);
    if (active >= 0) {
      rows[active].classList.add('is-active');
      rows[active].scrollIntoView({ block: 'nearest' });
      select.setAttribute('aria-activedescendant', rows[active].id);
    }
    document.addEventListener('keydown', onKeydown, true);
    // 打开后再定位一次：行是刚插进去的，第一次测到的高度可能还是 0。
    inst.place();
  }

  // 点一下原生 select：拦掉系统弹出层，改用我们的面板。
  document.addEventListener('mousedown', function (event) {
    var select = event.target.closest ? event.target.closest('select') : null;
    if (!isEnhanceable(select)) return;
    event.preventDefault();
    if (owner === select) { close(true); return; }
    open(select);
  }, true);

  // 键盘：Enter / 空格 / 上下箭头在原生 select 上都会弹系统层，一并接管。
  document.addEventListener('keydown', function (event) {
    var select = event.target.closest ? event.target.closest('select') : null;
    if (!isEnhanceable(select)) return;
    if (event.altKey && event.key === 'ArrowDown') { event.preventDefault(); open(select); return; }
    if (!inst && (event.key === 'Enter' || event.key === ' ' || event.key === 'ArrowDown' || event.key === 'ArrowUp')) {
      event.preventDefault();
      open(select);
    }
  }, true);

  // 面板里的行：mouseup 提交（mousedown 被文档级监听用掉，别在这里抢）。
  document.addEventListener('mouseup', function (event) {
    if (!inst) return;
    var row = event.target.closest ? event.target.closest('.wh-opt') : null;
    if (!row || !inst.panel.contains(row)) return;
    event.preventDefault();
    commit(rows.indexOf(row));
  }, true);

  window.whSelect = { close: close, open: open };
})();
