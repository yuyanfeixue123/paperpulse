/* 从账户页选择 LLM 厂商时自动带出 base_url 与推荐模型。
   原为内联 <script>，因启用严格 CSP（script-src 'self'）而外置。 */
function applyPreset(provider) {
  const sel = document.querySelector('select[name=provider]');
  if (!sel) return;
  const opt = sel.options[sel.selectedIndex];
  if (!opt) return;
  const base = document.getElementById('base_url');
  const model = document.getElementById('model');
  if (base && !base.value) base.value = opt.dataset.url || '';
  if (model && !model.value) model.value = opt.dataset.model || '';
}

document.addEventListener('DOMContentLoaded', function () {
  const sel = document.querySelector('select[name=provider]');
  if (sel && !document.getElementById('base_url').value) applyPreset(sel.value);
});
