/* 推送频率选择：选「自定义间隔」时才显示天数输入。
   与 form-enhance.js 分文件是为了让订阅页不必加载整包逻辑。 */

document.addEventListener('DOMContentLoaded', function () {
  const sel = document.querySelector('[data-cadence-select]');
  if (!sel) return;
  const box = document.querySelector('[data-cadence-days]');
  if (!box) return;

  function sync() {
    const on = sel.value === 'every_n_days';
    box.hidden = !on;
    const input = box.querySelector('input[name=cadence_days]');
    if (input) input.disabled = !on;
  }

  sel.addEventListener('change', sync);
  sync();
});
