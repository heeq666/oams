/* 下拉框：把原生 <select> 的展开层换成我们自己的玻璃面板（design-system-v1 §7.3）。
 *
 * 为什么还留着原生 <select>：它才是表单的主人。值是它提交的、name/required/
 * disabled 是它说了算、没有 JS 时它还照常能用；我们只接管「点开」这一步，
 * 把系统画的弹出层换成和菜单同一套玻璃（不落遮罩，见设计规范 §7.3）。所以增强是纯增量：脚本挂了，
 * 页面退化成系统下拉，不会坏。
 *
 * 用事件委托而不是初始化扫描：htmx 会把表格整块换掉，office-editor 还会在
 * 运行时造 <select>，逐个绑定必然漏。点在谁身上就给谁开。
 */
(function () {
  'use strict';

  var list = null;        // 当前展开的面板
  var owner = null;       // 面板属于哪个 <select>
  var rows = [];          // [{ el, value, disabled }]
  var active = -1;        // 键盘高亮行
  var typeahead = '';
  var typeaheadAt = 0;
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
        el: null,
        value: opt.value,
        label: (opt.label || opt.textContent || '').trim(),
        disabled: opt.disabled,
        selected: opt.selected,
        index: i,
        group: opt.parentNode && opt.parentNode.tagName === 'OPTGROUP' ? opt.parentNode.label : ''
      });
    }
    return out;
  }

  function position(select) {
    var rect = select.getBoundingClientRect();
    var margin = 8;
    var width = Math.max(rect.width, 168);
    width = Math.min(width, window.innerWidth - margin * 2);
    var left = Math.min(Math.max(rect.left, margin), window.innerWidth - width - margin);
    var natural = Math.min(list.scrollHeight, 320);
    var below = window.innerHeight - rect.bottom - margin;
    var above = rect.top - margin;
    var openAbove = below < Math.min(natural, 140) && above > below;
    list.style.width = width + 'px';
    list.style.left = left + 'px';
    list.style.top = (openAbove ? Math.max(margin, rect.top - natural - 6) : rect.bottom + 6) + 'px';
  }

  function setActive(index, focusSelect) {
    if (!rows.length) return;
    var step = index < active ? -1 : 1;
    var next = index;
    while (rows[next] && rows[next].disabled) next += step;
    if (!rows[next]) return;
    if (rows[active]) rows[active].classList.remove('is-active');
    active = next;
    rows[active].classList.add('is-active');
    rows[active].scrollIntoView({ block: 'nearest' });
    if (focusSelect !== false) owner.focus({ preventScroll: true });
  }

  function commit(index) {
    var row = rows[index];
    if (!row || row.disabled) return;
    var select = owner;
    select.value = row.dataset.value;
    select.dispatchEvent(new Event('input', { bubbles: true }));
    select.dispatchEvent(new Event('change', { bubbles: true }));
    close();
    select.focus({ preventScroll: true });
  }

  function close() {
    if (!list) return;
    document.removeEventListener('keydown', onKeydown, true);
    document.removeEventListener('mousedown', onOutside, true);
    document.removeEventListener('scroll', onScroll, true);
    window.removeEventListener('resize', close);
    if (owner) {
      owner.classList.remove('is-open');
      owner.setAttribute('aria-expanded', 'false');
      owner.removeAttribute('aria-controls');
      owner.removeAttribute('aria-activedescendant');
    }
    list.remove();
    list = null; owner = null; rows = []; active = -1;
  }

  function onOutside(event) {
    if (!list) return;
    var path = event.composedPath ? event.composedPath() : [];
    if (path.indexOf(list) >= 0) return;
    close();          // 点外面先收面板，这一次点击照常落到页面上
  }

  function onScroll(event) {
    if (!list) return;
    if (list.contains(event.target)) return;   // 面板自己滚动不算
    close();
  }

  function onKeydown(event) {
    if (!list) return;
    var key = event.key;
    if (key === 'Escape') { event.preventDefault(); var s = owner; close(); s.focus(); return; }
    if (key === 'Tab') { close(); return; }
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
    close();
    var options = optionsOf(select);
    if (!options.length) return;
    list = document.createElement('div');
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
      row.setAttribute('role', 'option');
      row.dataset.index = i;
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
    document.body.appendChild(list);
    owner = select;
    owner.classList.add('is-open');
    owner.setAttribute('aria-expanded', 'true');
    owner.setAttribute('aria-controls', list.id);
    position(select);
    if (active >= 0) {
      rows[active].classList.add('is-active');
      rows[active].scrollIntoView({ block: 'nearest' });
    }
    document.addEventListener('keydown', onKeydown, true);
    document.addEventListener('mousedown', onOutside, true);
    document.addEventListener('scroll', onScroll, true);
    window.addEventListener('resize', close);
    select.focus({ preventScroll: true });
  }

  // 点一下原生 select：拦掉系统弹出层，改用我们的面板。
  document.addEventListener('mousedown', function (event) {
    var select = event.target.closest ? event.target.closest('select') : null;
    if (!isEnhanceable(select)) return;
    event.preventDefault();
    if (owner === select) { close(); return; }
    open(select);
  }, true);

  // 键盘：Enter / 空格 / 上下箭头在原生 select 上都会弹系统层，一并接管。
  document.addEventListener('keydown', function (event) {
    var select = event.target.closest ? event.target.closest('select') : null;
    if (!isEnhanceable(select)) return;
    if (event.altKey && event.key === 'ArrowDown') { event.preventDefault(); open(select); return; }
    if (event.key === 'Enter' || event.key === ' ' || event.key === 'ArrowDown' || event.key === 'ArrowUp') {
      event.preventDefault();
      open(select);
    }
  }, true);

  // 面板里的行：mouseup 提交（mousedown 被文档级监听用掉，别在这里抢）。
  document.addEventListener('mouseup', function (event) {
    if (!list) return;
    var row = event.target.closest ? event.target.closest('.wh-opt') : null;
    if (!row || !list.contains(row)) return;
    event.preventDefault();
    commit(rows.indexOf(row));
  }, true);

  // htmx 换块之前先收面板，否则它会在旧节点上悬着。
  document.addEventListener('htmx:beforeSwap', close);
  window.whSelect = { close: close, open: open };
})();
