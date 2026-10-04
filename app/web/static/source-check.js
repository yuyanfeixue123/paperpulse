/* 后台数据源连通性测试：提交表单到 /admin/sources/check 并提示结果。
   原为内联 <script>，因启用严格 CSP（script-src 'self'）而外置。 */
async function checkSource(form, key) {
  if (typeof event !== 'undefined') event.preventDefault();
  const fd = new FormData(form);
  const res = await fetch('/admin/sources/check', { method: 'POST', body: fd });
  const data = await res.json();
  alert(key + '：' + data.message);
  return false;
}
