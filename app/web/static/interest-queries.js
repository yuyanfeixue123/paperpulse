/* 订阅第二步：把「arXiv / OpenAlex 检索式」两个输入框同步成隐藏的
   queries JSON 字段。原为内联 <script>，因启用严格 CSP（script-src 'self'）而外置。 */
function syncQueries() {
  const q = {};
  const a = document.getElementById('q_arxiv').value.trim();
  const o = document.getElementById('q_openalex').value.trim();
  if (a) q.arxiv = a;
  if (o) q.openalex = o;
  document.getElementById('queries').value = JSON.stringify(q);
}
