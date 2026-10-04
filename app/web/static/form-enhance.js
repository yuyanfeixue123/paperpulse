/* 关键词点击选择 + 注册密码实时校验。
   外置而非内联，是为了配合 CSP 的 script-src 'self'（禁止内联脚本）。 */

/* ---------------------------------------------------------- 关键词标签 */

/**
 * 把一个逗号分隔的输入框变成「点击标签」选择器。
 *
 * 设计取舍：保留一个可自由输入的文本框 —— 论文关键词千奇百怪，
 * 预置词表永远覆盖不全；标签只是**快捷入口**，不是唯一输入方式。
 * 标签与输入框双向同步：点标签即加入输入框，改输入框即重建标签。
 *
 * @param {string} inputSel  承载逗号分隔关键词的输入框选择器
 * @param {HTMLElement} picker  标签容器
 * @param {string[]} presets  预置候选词
 */
function setupKeywordPicker(inputSel, picker, presets) {
  const input = document.querySelector(inputSel);
  if (!input || !picker) return;
  const selected = new Set();

  const split = (raw) =>
    (raw || '')
      .split(/[,，;；\n]/)
      .map((s) => s.trim())
      .filter(Boolean);

  const sync = () => {
    split(input.value).forEach((k) => selected.add(k));
    render();
  };

  function render() {
    picker.innerHTML = '';
    const all = Array.from(new Set([...presets, ...selected]));
    all.forEach((kw) => {
      const chip = document.createElement('button');
      chip.type = 'button';
      chip.className = 'chip' + (selected.has(kw) ? ' on' : '');
      chip.textContent = (selected.has(kw) ? '✓ ' : '+ ') + kw;
      chip.addEventListener('click', () => {
        if (selected.has(kw)) selected.delete(kw);
        else selected.add(kw);
        // 用英文逗号拼接：中文逗号会让下游 split() 漏掉最后一个词
        input.value = Array.from(selected).join(', ');
        render();
      });
      picker.appendChild(chip);
    });
  }

  input.addEventListener('input', sync);
  sync();
}

/* ------------------------------------------------------------ 密码校验 */

/** 与后端 app/core/security.py 的 password_strength_ok 保持一致 */
function passwordIssues(v) {
  const out = [];
  if (v.length < 10) out.push('至少 10 位');
  if (!/[a-z]/.test(v)) out.push('需含小写字母');
  if (!/[A-Z]/.test(v)) out.push('需含大写字母');
  if (!/[0-9]/.test(v)) out.push('需含数字');
  return out;
}

function setupPasswordLiveCheck(inputSel, boxSel) {
  const input = document.querySelector(inputSel);
  const box = document.querySelector(boxSel);
  if (!input || !box) return;

  const rules = [
    { test: (v) => v.length >= 10, text: '至少 10 位' },
    { test: (v) => /[a-z]/.test(v), text: '含小写字母' },
    { test: (v) => /[A-Z]/.test(v), text: '含大写字母' },
    { test: (v) => /[0-9]/.test(v), text: '含数字' },
  ];

  function render() {
    const v = input.value;
    box.innerHTML = '';
    if (!v) {
      box.className = 'pw-rules';
      return;
    }
    rules.forEach((r) => {
      const ok = r.test(v);
      const span = document.createElement('span');
      span.className = ok ? 'pw-ok' : 'pw-bad';
      span.textContent = (ok ? '✓ ' : '✗ ') + r.text;
      box.appendChild(span);
    });
    box.className = 'pw-rules active';
  }

  input.addEventListener('input', render);
  render();
}

document.addEventListener('DOMContentLoaded', function () {
  // 注册页：密码实时校验
  setupPasswordLiveCheck('#reg-password', '#pw-rules');

  // 订阅配置页：关键词点击选择
  const inc = document.querySelector('#inc-kw');
  const exc = document.querySelector('#exc-kw');
  if (inc && exc) {
    const presets = JSON.parse(document.getElementById('kw-presets').textContent);
    setupKeywordPicker('#inc-kw', document.getElementById('inc-kw-picker'), presets);
    setupKeywordPicker('#exc-kw', document.getElementById('exc-kw-picker'), presets.slice(0, 24));
  }
});
