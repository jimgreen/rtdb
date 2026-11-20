// Keep directory identity in each tab's URL, including uploads and task polling.
const workspacePath = new URLSearchParams(location.search).get('workspace') || '';
let workspaceScope = null;
function workspaceUrl(value) {
  const url = new URL(value, location.href);
  if (url.origin === location.origin && url.pathname.startsWith('/api/') && workspacePath && !url.searchParams.has('workspace')) {
    url.searchParams.set('workspace', workspacePath);
  }
  return url.href;
}
function workspaceFetch(value, options) { return fetch(workspaceUrl(value), options); }
function configureWorkspaceScope(info) {
  workspaceScope = info.strict_root ? JSON.stringify([info.root_id, info.data_dir]) : null;
}
function workspaceStorageKey(key) {
  if (workspaceScope) return `${key}:root:${workspaceScope}`;
  return workspacePath ? `${key}:workspace:${workspacePath}` : key;
}

const workspaceTree = {
  nodes: new Map(), roots: [], expanded: new Set(['.']), searchCollapsed: new Set(),
  storageKey: null, strictRoot: false, currentPath: workspacePath,
};
function workspaceParent(path) { return path === '.' || path === '' ? null : path.includes('/') ? path.slice(0, path.lastIndexOf('/')) : '.'; }
function workspaceLabel(path) {
  if (workspaceTree.strictRoot) return path === '.' ? '根目录（.）' : path;
  return path === '' ? '默认数据库目录' : path === '.' ? 'workspace（根目录）' : `workspace / ${path}`;
}

function bindWorkspacePicker() {
  const dialog = el('database-file-dialog');
  const trigger = el('workspace-picker-trigger');
  const picker = el('workspace-picker');
  const search = el('workspace-search');

  function positionPicker() {
    const anchor = trigger.getBoundingClientRect();
    const viewportWidth = document.documentElement.clientWidth;
    const viewportHeight = document.documentElement.clientHeight;
    const margin = 8, gap = 4, desiredHeight = 340;
    const below = viewportHeight - margin - anchor.bottom - gap;
    const above = anchor.top - gap - margin;
    const openAbove = below < desiredHeight && above > below;
    const height = Math.min(desiredHeight, Math.max(0, openAbove ? above : below));
    const width = Math.min(anchor.width, viewportWidth - margin * 2);
    picker.style.width = `${width}px`;
    picker.style.height = `${height}px`;
    picker.style.left = `${Math.max(margin, Math.min(anchor.left, viewportWidth - margin - width))}px`;
    picker.style.top = `${openAbove ? anchor.top - gap - height : anchor.bottom + gap}px`;
  }

  picker.addEventListener('beforetoggle', event => {
    if (event.newState === 'open') positionPicker();
  });
  const repositionIfOpen = () => {
    if (picker.matches(':popover-open')) positionPicker();
  };
  window.addEventListener('resize', repositionIfOpen);
  dialog.addEventListener('scroll', repositionIfOpen);
  dialog.addEventListener('close', () => picker.hidePopover());

  search.addEventListener('input', () => { workspaceTree.searchCollapsed.clear(); renderWorkspaceTree(); });
  search.addEventListener('keydown', event => {
    if (event.key === 'Enter') event.preventDefault();
    if (event.key === 'Escape' && search.value) {
      event.preventDefault();
      search.value = '';
      workspaceTree.searchCollapsed.clear();
      renderWorkspaceTree();
    }
  });
}

async function loadWorkspaceDirectories() {
  const result = await request('workspaces');
  workspaceTree.strictRoot = result.strict_root === true;
  workspaceTree.currentPath = workspacePath || (workspaceTree.strictRoot ? result.default_data_dir : '');
  workspaceTree.nodes = new Map();
  if (!workspaceTree.strictRoot) {
    workspaceTree.nodes.set('', { path: '', label: '默认数据库目录', children: [] });
  }
  for (const item of result.directories) {
    const label = item.path === '.' && !workspaceTree.strictRoot ? 'workspace' : workspaceLabel(item.path).split('/').pop();
    workspaceTree.nodes.set(item.path, { ...item, label, children: [] });
  }
  workspaceTree.roots = [];
  for (const item of workspaceTree.nodes.values()) {
    const parent = workspaceTree.nodes.get(workspaceParent(item.path));
    if (parent) parent.children.push(item);
    else workspaceTree.roots.push(item);
  }
  if (!workspaceTree.storageKey) {
    workspaceTree.storageKey = `rtdb-workspace-tree:${result.root_id || result.workspace_root}`;
    try {
      const saved = JSON.parse(sessionStorage.getItem(workspaceTree.storageKey));
      if (Array.isArray(saved)) workspaceTree.expanded = new Set(saved);
    } catch { /* Expansion remains usable without browser storage. */ }
    for (let parent = workspaceParent(workspaceTree.currentPath); parent !== null; parent = workspaceParent(parent)) workspaceTree.expanded.add(parent);
  }
  el('workspace-current').textContent = workspaceLabel(workspaceTree.currentPath);
  el('workspace-current').title = workspaceTree.strictRoot
    ? `相对于根目录：${workspaceTree.currentPath}`
    : `工作区根目录：${result.workspace_root}\n默认数据目录：${result.default_data_dir}`;
  renderWorkspaceTree();
}

function renderWorkspaceTree() {
  const target = el('workspace-tree'), query = el('workspace-search').value.trim().toLowerCase().replaceAll('\\', '/');
  const visible = new Set(), matches = new Set();
  if (query) {
    for (const item of workspaceTree.nodes.values()) {
      if (!workspaceLabel(item.path).toLowerCase().includes(query)) continue;
      matches.add(item.path);
      for (let path = item.path; path !== null; path = workspaceParent(path)) visible.add(path);
    }
  }
  const makeList = items => {
    const list = document.createElement('ul');
    list.className = 'workspace-tree-list';
    for (const item of items) {
      if (query && !visible.has(item.path)) continue;
      const entry = document.createElement('li'), row = document.createElement('div');
      row.className = 'workspace-tree-row';
      const children = item.children.filter(child => !query || visible.has(child.path));
      const expanded = query ? !workspaceTree.searchCollapsed.has(item.path) : workspaceTree.expanded.has(item.path);
      if (children.length) {
        const toggle = document.createElement('button');
        toggle.type = 'button'; toggle.className = 'workspace-tree-toggle'; toggle.textContent = expanded ? '▾' : '▸';
        toggle.setAttribute('aria-label', `${expanded ? '折叠' : '展开'} ${workspaceLabel(item.path)}`);
        toggle.setAttribute('aria-expanded', String(expanded));
        toggle.onclick = () => {
          if (query) {
            if (expanded) workspaceTree.searchCollapsed.add(item.path); else workspaceTree.searchCollapsed.delete(item.path);
          } else {
            if (expanded) workspaceTree.expanded.delete(item.path); else workspaceTree.expanded.add(item.path);
            try { sessionStorage.setItem(workspaceTree.storageKey, JSON.stringify([...workspaceTree.expanded])); } catch {}
          }
          const scroll = target.scrollTop;
          renderWorkspaceTree(); target.scrollTop = scroll;
          [...target.querySelectorAll('button[data-toggle-path]')].find(button => button.dataset.togglePath === item.path)?.focus({ preventScroll: true });
        };
        toggle.dataset.togglePath = item.path; row.append(toggle);
      } else {
        const spacer = document.createElement('span'); spacer.className = 'workspace-tree-spacer'; row.append(spacer);
      }
      const choose = document.createElement('button');
      choose.type = 'button'; choose.className = 'workspace-tree-choice'; choose.title = workspaceLabel(item.path);
      choose.setAttribute('aria-label', `选择目录 ${workspaceLabel(item.path)}`);
      if (item.path === workspaceTree.currentPath) { choose.classList.add('selected'); choose.setAttribute('aria-current', 'location'); }
      const name = document.createElement('span'); name.textContent = item.label; name.className = 'workspace-tree-name';
      if (matches.has(item.path)) name.classList.add('match');
      choose.append(name);
      if (item.database_count !== undefined) {
        const count = document.createElement('span'); count.className = 'workspace-tree-count'; count.textContent = `${item.database_count} 个库`; choose.append(count);
      }
      choose.onclick = () => run(() => openWorkspaceDirectory(item.path));
      row.append(choose); entry.append(row);
      if (children.length && expanded) entry.append(makeList(children));
      list.append(entry);
    }
    return list;
  };
  target.replaceChildren(makeList(workspaceTree.roots));
  if (query && !matches.size) { const empty = document.createElement('p'); empty.className = 'empty'; empty.textContent = '没有匹配的目录'; target.append(empty); }
  el('workspace-search-status').textContent = query ? `${matches.size} 个匹配目录` : '点击箭头展开 / 折叠，点击目录名称立即刷新文件列表';
}

async function openWorkspaceDirectory(selected) {
  if (selected === workspaceTree.currentPath) {
    await loadDatabaseFiles(); el('workspace-picker').hidePopover(); return;
  }
  if (backgroundTasks.pending() || databaseUploads.active || csvImports.active || efileImports.active) {
    throw new Error('请等待后台任务完成或暂停上传后再切换目录');
  }
  if (!await discardSchema() || !await discardCellEdits()) return;
  const url = new URL(location.href);
  if (selected) url.searchParams.set('workspace', selected);
  else url.searchParams.delete('workspace');
  url.searchParams.delete('database');
  url.searchParams.set('choose_database', '1');
  location.assign(url.href);
}
