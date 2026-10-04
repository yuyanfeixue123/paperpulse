/* 论文卡片增强：外链徽标 + 复制 BibTeX。

   外链（alphaXiv 讨论 / Connected Papers 图谱）都是**按标识拼 URL**，
   不需要任何 API 调用 —— 服务端什么都不用做，链接在前端构造即可。
   复制 BibTeX 同样是纯前端：字段我们本来就有。

   外置文件而非内联脚本：配合 CSP 的 script-src 'self'。 */

/** 从 base64 还原论文字典。
    服务端刻意用 base64 而非直接内联 JSON：Jinja 的 tojson 会把内部引号
    转义成 \"，而 HTML 属性解析器不认那个反斜杠 —— 属性会在第一个引号处
    被截断。标题里带引号（很常见）就会触发。 */
function decodePaper(b64) {
  if (!b64) return null;
  var bin = atob(b64);
  var bytes = new Uint8Array(bin.length);
  for (var i = 0; i < bin.length; i++) bytes[i] = bin.charCodeAt(i);
  return JSON.parse(new TextDecoder('utf-8').decode(bytes));
}

/** 从 DOI / arXiv ID 推出可用的外链。返回 {key, label, url} 列表。 */
function paperLinks(p) {
  const out = [];
  const arxiv = (p.arxiv_id || '').trim();
  const doi = (p.doi || '').trim();

  if (arxiv) {
    // alphaXiv 有社区讨论与解释，无公开 API 但 URL 可构造
    out.push({
      key: 'alphaxiv',
      label: 'alphaXiv 解读',
      url: 'https://www.alphaxiv.org/abs/' + encodeURIComponent(arxiv),
    });
  }
  if (doi) {
    // Connected Papers 靠 DOI 建图谱
    out.push({
      key: 'connected',
      label: '引用图谱',
      url: 'https://www.connectedpapers.com/main?doi=' + encodeURIComponent(doi),
    });
  } else if (arxiv) {
    out.push({
      key: 'connected',
      label: '引用图谱',
      url: 'https://www.connectedpapers.com/main?arxivId=' + encodeURIComponent(arxiv),
    });
  }
  return out;
}

/** 生成 BibTeX。字段顺序按常用引用格式，缺失项自动省略。 */
function toBibTeX(p) {
  const first = (p.authors || [])[0] || '';
  const firstSurname = first.split(' ').pop() || '';
  const year = (p.published_at || '').slice(0, 4) || 'n.d.';
  const key = (firstSurname || 'anon') + year + (p.id || '');

  const fields = [
    ['title', p.title || ''],
    ['author', (p.authors || []).join(' and ')],
    ['year', year],
    ['journal', p.venue || ''],
    ['doi', p.doi || ''],
    ['url', p.url || (p.doi ? 'https://doi.org/' + p.doi : '')],
    ['eprint', p.arxiv_id || ''],
    ['archivePrefix', p.arxiv_id ? 'arXiv' : ''],
  ];
  const body = fields
    .filter(function (f) { return f[1]; })
    .map(function (f) {
      // 大括号包裹 title，保留其中的大写字母（{BERT} 而非 {BERT} 被降为 bert）
      const v = f[0] === 'title' ? '{' + f[1] + '}' : f[1];
      return '  ' + f[0] + ' = {' + v + '}';
    })
    .join(',\n');
  return '@article{' + key + ',\n' + body + '\n}';
}

function setupPaperCards() {
  document.querySelectorAll('[data-paper-card]').forEach(function (card) {
    let p;
    try {
      p = decodePaper(card.getAttribute('data-paper'));
    } catch (e) {
      return; // 数据坏了就不渲染外链，不影响卡片其余部分
    }

    // 外链
    const slot = card.querySelector('[data-links]');
    if (slot) {
      const links = paperLinks(p);
      links.forEach(function (l) {
        const a = document.createElement('a');
        a.className = 'btn secondary tiny';
        a.href = l.url;
        a.target = '_blank';
        a.rel = 'noopener';
        a.textContent = l.label;
        slot.appendChild(a);
      });
      if (links.length) slot.hidden = false;
    }

    // 代码仓库（来自 HF Daily Papers 的 githubRepo，作者自填）
    if (p.github_repo) {
      const slot = card.querySelector('[data-code]');
      if (slot) {
        const a = document.createElement('a');
        a.className = 'btn secondary tiny';
        a.href = 'https://github.com/' + p.github_repo;
        a.target = '_blank';
        a.rel = 'noopener';
        a.textContent = p.github_stars > 0
          ? '代码 ★' + p.github_stars
          : '代码';
        slot.appendChild(a);
        slot.hidden = false;
      }
    }

    // 复制 BibTeX
    const copy = card.querySelector('[data-copy-bibtex]');
    if (copy) {
      copy.addEventListener('click', function (ev) {
        ev.preventDefault();
        const text = toBibTeX(p);
        const done = function (ok) {
          copy.textContent = ok ? '已复制 ✓' : '复制失败';
          window.setTimeout(function () { copy.textContent = 'BibTeX'; }, 1600);
        };
        if (navigator.clipboard && navigator.clipboard.writeText) {
          navigator.clipboard.writeText(text).then(
            function () { done(true); },
            function () { done(fallbackCopy(text)); }
          );
        } else {
          done(fallbackCopy(text));
        }
      });
    }
  });
}

/** clipboard API 不可用时的兜底（HTTP 非 localhost 下 clipboard 常被禁用）。 */
function fallbackCopy(text) {
  try {
    const ta = document.createElement('textarea');
    ta.value = text;
    ta.setAttribute('readonly', '');
    ta.style.position = 'fixed';
    ta.style.left = '-9999px';
    document.body.appendChild(ta);
    ta.select();
    const ok = document.execCommand('copy');
    document.body.removeChild(ta);
    return ok;
  } catch (e) {
    return false;
  }
}

document.addEventListener('DOMContentLoaded', setupPaperCards);
