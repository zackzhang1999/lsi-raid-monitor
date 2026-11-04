(() => {
  const $ = (selector, root = document) => root.querySelector(selector);
  const $$ = (selector, root = document) => Array.from(root.querySelectorAll(selector));

  const menuButton = $('.menu-toggle');
  const nav = $('#site-nav');
  if (menuButton && nav) {
    menuButton.addEventListener('click', () => {
      const open = nav.classList.toggle('open');
      menuButton.setAttribute('aria-expanded', String(open));
    });
    $$('#site-nav a').forEach(link => link.addEventListener('click', () => {
      nav.classList.remove('open');
      menuButton.setAttribute('aria-expanded', 'false');
    }));
  }

  async function copyText(text, button) {
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
      const original = button.textContent;
      button.textContent = '已复制';
      setTimeout(() => { button.textContent = original; }, 1600);
    } catch (error) {
      button.textContent = '复制失败';
    }
  }

  let latestRelease = null;
  const copySha = $('[data-copy-sha]');
  if (copySha) {
    copySha.addEventListener('click', () => {
      copyText(latestRelease?.sha256 || $('[data-latest-sha]')?.textContent || '', copySha);
    });
  }

  const copyCommand = $('[data-copy-command]');
  if (copyCommand) {
    copyCommand.addEventListener('click', () => {
      const text = $('#install-command code')?.textContent || '';
      copyText(text, copyCommand);
    });
  }

  function formatBytes(value) {
    const bytes = Number(value);
    if (!Number.isFinite(bytes) || bytes <= 0) return '—';
    if (bytes >= 1024 ** 3) return `${(bytes / 1024 ** 3).toFixed(1)} GB`;
    if (bytes >= 1024 ** 2) return `${(bytes / 1024 ** 2).toFixed(1)} MB`;
    return `${Math.ceil(bytes / 1024)} KB`;
  }

  function releaseDate(release) {
    return release?.date || release?.published_at?.slice(0, 10) || '—';
  }

  function populateRelease(release) {
    if (!release) return;
    latestRelease = release;
    $$('[data-latest-version]').forEach(el => { el.textContent = release.version || '—'; });
    $$('[data-latest-date]').forEach(el => { el.textContent = releaseDate(release); });
    $$('[data-latest-size]').forEach(el => { el.textContent = formatBytes(release.size); });
    $$('[data-latest-sha]').forEach(el => { el.textContent = release.sha256 || '生成中'; });
    $$('[data-download]').forEach(link => {
      link.href = release.file ? `/download/${encodeURIComponent(release.file)}` : '#download';
    });
    const command = $('#install-command code');
    if (command && release.file) {
      command.textContent = [
        `sha256sum ${release.file}`,
        `tar -xzf ${release.file}`,
        `cd ${release.file.replace(/\.tar\.gz$/, '')}`,
        'sudo bash install.sh \\',
        '  --storcli-source /path/to/storcli64',
      ].join('\n');
    }
  }

  function renderHistory(releases) {
    const box = $('[data-release-history]');
    if (!box) return;
    const rows = (releases || []).map(release => {
      const row = document.createElement('div');
      row.className = 'release-row';
      const values = [
        release.version || '—',
        releaseDate(release),
        formatBytes(release.size),
      ];
      values.forEach(value => {
        const cell = document.createElement('span');
        cell.textContent = value;
        row.appendChild(cell);
      });
      const action = document.createElement('span');
      if (release.file) {
        const link = document.createElement('a');
        link.href = `/download/${encodeURIComponent(release.file)}`;
        link.textContent = '下载';
        action.appendChild(link);
      } else {
        action.textContent = '—';
      }
      row.appendChild(action);
      return row;
    });
    box.append(...rows);
  }

  fetch('/api/releases', { credentials: 'same-origin' })
    .then(response => {
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      return response.json();
    })
    .then(data => {
      const releases = Array.isArray(data.releases) ? data.releases : [];
      const latest = data.latest || releases[0]?.version;
      const release = releases.find(item => item.version === latest) || releases[0];
      populateRelease(release);
      renderHistory(releases);
    })
    .catch(() => {
      $$('[data-latest-date], [data-latest-size], [data-latest-sha]').forEach(el => {
        el.textContent = '发布信息暂不可用';
      });
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
})();
