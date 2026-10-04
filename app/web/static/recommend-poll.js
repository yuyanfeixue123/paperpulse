/* 轮询「立刻推荐」任务进度，完成后自动刷新页面。
   「立刻推荐」已改为异步入队（每个订阅要跑召回 + LLM 批量打分，
   同步等会把浏览器卡死），所以页面需要自己盯着任务状态。 */

(function () {
  var box = document.querySelector('[data-recommend-poll]');
  if (!box) return;
  var taskId = box.getAttribute('data-recommend-poll');
  if (!taskId || taskId === '0') return;

  var tries = 0;
  var MAX_TRIES = 180; // 约 3 分钟，与后端任务超时同量级

  function poll() {
    tries += 1;
    if (tries > MAX_TRIES) {
      box.textContent = '推荐仍在后台进行，可以稍后刷新本页查看。';
      return;
    }
    fetch('/feed/status?task_id=' + encodeURIComponent(taskId), {
      headers: { Accept: 'application/json' },
    })
      .then(function (r) { return r.json(); })
      .then(function (d) {
        if (d.state === 'done') {
          box.textContent = '推荐已生成，正在刷新…';
          window.location.reload();
        } else if (d.state === 'failed') {
          box.textContent = '生成失败：' + (d.error || '未知原因');
        } else if (d.state !== 'unknown') {
          window.setTimeout(poll, 2000);
        } else {
          // 任务查不到（已过期或不属于当前用户）：静默停止轮询
          box.textContent = '';
        }
      })
      .catch(function () {
        // 网络抖动不放弃，继续轮询到次数上限
        window.setTimeout(poll, 3000);
      });
  }

  window.setTimeout(poll, 1500);
})();
