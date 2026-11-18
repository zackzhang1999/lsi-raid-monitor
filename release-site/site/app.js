(() => {
  const $ = (selector, root = document) => root.querySelector(selector);
  const $$ = (selector, root = document) => Array.from(root.querySelectorAll(selector));
  const esc = value => String(value ?? '').replace(/[&<>"']/g, char => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
  }[char]));

  const menuButton = $('.menu-toggle');
  const mainNav = $('#main-nav');
  if (menuButton && mainNav) {
    menuButton.addEventListener('click', () => {
      const open = mainNav.classList.toggle('open');
      menuButton.setAttribute('aria-expanded', String(open));
    });
    $$('#main-nav a').forEach(link => link.addEventListener('click', () => {
      mainNav.classList.remove('open');
      menuButton.setAttribute('aria-expanded', 'false');
    }));
  }

  let toastTimer = 0;
  function toast(message) {
    const el = $('#toast');
    if (!el) return;
    el.textContent = message;
    el.classList.add('show');
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => el.classList.remove('show'), 1800);
  }

  async function copyText(text, button, success) {
    if (!text) return;
    try {
      if (navigator.clipboard && window.isSecureContext) {
        await navigator.clipboard.writeText(text);
      } else {
        const area = document.createElement('textarea');
        area.value = text;
        area.style.position = 'fixed';
        area.style.opacity = '0';
        document.body.appendChild(area);
        area.select();
        document.execCommand('copy');
        area.remove();
      }
      const original = button?.textContent;
      if (button) {
        button.textContent = success;
        button.classList.add('is-copied');
        setTimeout(() => {
          button.textContent = original;
          button.classList.remove('is-copied');
        }, 2200);
      }
      toast(success);
    } catch (_) {
      toast('复制失败，请手动选择文本');
    }
  }

  const formatSize = bytes => {
    const value = Number(bytes);
    if (!Number.isFinite(value) || value <= 0) return '—';
    if (value >= 1024 ** 3) return `${(value / 1024 ** 3).toFixed(1)} GB`;
    if (value >= 1024 ** 2) return `${(value / 1024 ** 2).toFixed(1)} MB`;
    return `${Math.ceil(value / 1024)} KB`;
  };
  const formatDate = value => value || '—';

  let latestRelease = null;
  let latestUrl = '';

  function renderHistory(releases) {
    const box = $('[data-release-history]');
    if (!box) return;
    if (!releases.length) {
      box.innerHTML = '<div class="empty">暂无发布包</div>';
      return;
    }
    box.innerHTML = `
      <div class="release-row head"><span>版本</span><span>说明</span><span>大小</span><span>日期</span><span>下载</span></div>
      ${releases.map((release, index) => `
        <div class="release-row">
          <span class="version">v${esc(release.version)}${index === 0 ? '<span class="latest">LATEST</span>' : ''}</span>
          <span class="notes" title="${esc(release.notes || '')}">${esc(release.notes || 'DataMax-LSI 完整安装包')}</span>
          <span class="size">${esc(formatSize(release.size))}</span>
          <span class="date">${esc(formatDate(release.date))}</span>
          <a href="/download/${encodeURIComponent(release.file)}">下载</a>
        </div>
      `).join('')}`;
  }

  async function loadRelease() {
    try {
      const response = await fetch('/api/releases', { cache: 'no-store' });
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      const data = await response.json();
      const releases = Array.isArray(data.releases) ? data.releases : [];
      const latest = releases.find(item => item.version === data.latest) || releases[0];
      if (!latest) {
        renderHistory([]);
        return;
      }
      latestRelease = latest;
      latestUrl = new URL(`/download/${encodeURIComponent(latest.file)}`, location.origin).href;
      $$('[data-latest-version]').forEach(el => { el.textContent = latest.version; });
      $$('[data-latest-size]').forEach(el => { el.textContent = formatSize(latest.size); });
      $$('[data-latest-date]').forEach(el => { el.textContent = formatDate(latest.date); });
      $$('[data-latest-sha]').forEach(el => { el.textContent = latest.sha256 || '—'; });
      $$('[data-download]').forEach(el => { el.href = latestUrl; });

      const title = $('#dl-title');
      const subtitle = $('#dl-sub');
      if (title) title.textContent = `完整安装包 · ${latest.version}`;
      if (subtitle) {
        subtitle.textContent = latest.notes || '包含应用源码、Web 前端、安装器、运维脚本和使用文档。';
      }
      const command = $('#install-command code');
      if (command) {
        command.innerHTML = `<span class="c"># 解压后校验并安装</span>\n`
          + `sha256sum ${esc(latest.file)}\n`
          + `tar -xzf ${esc(latest.file)}\n`
          + `cd ${esc(latest.file.replace(/\.tar\.gz$/, ''))}\n`
          + `sudo bash install.sh \\\n`
          + `  --storcli-source /path/to/storcli64`;
      }
      renderHistory(releases);
    } catch (error) {
      renderHistory([]);
      const subtitle = $('#dl-sub');
      if (subtitle) subtitle.textContent = `发布信息读取失败：${error.message}`;
    }
  }

  $$('[data-copy-sha]').forEach(button => {
    button.addEventListener('click', () => copyText(latestRelease?.sha256 || '', button, 'SHA-256 已复制'));
  });
  $$('[data-copy-url]').forEach(button => {
    button.addEventListener('click', () => copyText(latestUrl, button, '下载地址已复制'));
  });
  $$('[data-copy-command]').forEach(button => {
    button.addEventListener('click', () => copyText($('#install-command code')?.textContent || '', button, '安装命令已复制'));
  });

  const tocLinks = $$('.manual-toc a');
  if (tocLinks.length && 'IntersectionObserver' in window) {
    const byId = new Map(tocLinks.map(link => [link.getAttribute('href')?.slice(1), link]));
    const observer = new IntersectionObserver(entries => {
      entries.forEach(entry => {
        if (!entry.isIntersecting) return;
        tocLinks.forEach(link => link.classList.remove('active'));
        byId.get(entry.target.id)?.classList.add('active');
      });
    }, { rootMargin: '-12% 0px -76% 0px', threshold: 0 });
    $$('.manual-article section[id]').forEach(section => observer.observe(section));
  }

  const search = $('.manual-search');
  if (search) {
    search.addEventListener('input', () => {
      const query = search.value.trim().toLowerCase();
      $$('.manual-article section[id]').forEach(section => {
        section.hidden = Boolean(query) && !section.textContent.toLowerCase().includes(query);
      });
      tocLinks.forEach(link => {
        const section = $(link.getAttribute('href'));
        link.hidden = Boolean(section?.hidden);
      });
    });
  }

  loadRelease();
})();
