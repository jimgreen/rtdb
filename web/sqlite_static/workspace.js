// Keep directory identity in each tab's URL, including uploads and task polling.
const workspacePath = new URLSearchParams(location.search).get('workspace') || '';
let workspaceScope = null;
let selectionKey = null, selectionDirectory = null;
let activeEfileSessionId = null;
function setActiveEfileSession(id) {
  activeEfileSessionId = id ? String(id) : null;
}
function readWorkspaceSelection() {
  try {
    const saved = JSON.parse(localStorage.getItem(selectionKey) || '{}');
    return saved && typeof saved === 'object' && !Array.isArray(saved) ? saved : {};
  } catch { return {}; }
}
function rememberWorkspaceSelection(filename) {
  if (!selectionKey || activeEfileSessionId) return;
  const saved = readWorkspaceSelection();
  saved.workspace = selectionDirectory;
  if (filename !== undefined) {
    saved.databases = { ...saved.databases, [selectionDirectory]: filename };
  }
  try { localStorage.setItem(selectionKey, JSON.stringify(saved)); }
  catch { notice('浏览器未能保存目录和数据库选择，下次打开可能需要重新选择'); }
  const url = new URL(location.href);
  if (filename !== undefined) {
    if (filename) url.searchParams.set('database', filename); else url.searchParams.delete('database');
  }
  url.searchParams.delete('choose_database');
  if (url.href !== location.href) history.replaceState(history.state, '', url.href);
}
function rememberedDatabase() {
  const databases = readWorkspaceSelection().databases;
  const filename = databases && Object.hasOwn(databases, selectionDirectory) ? databases[selectionDirectory] : null;
  return typeof filename === 'string' ? filename : null;
}
function rememberWorkspaceFolder(directory) {
  if (!selectionKey) return;
  const saved = readWorkspaceSelection();
  saved.browsing = { workspace: selectionDirectory, directory };
  try { localStorage.setItem(selectionKey, JSON.stringify(saved)); } catch { /* Optional preference. */ }
}
async function restoreWorkspaceSelection(info) {
  selectionKey = 'rtdb-last-selection-v1:' + (info.root_id || 'default');
  selectionDirectory = info.strict_root ? info.data_dir : workspacePath;
  workspaceTree.currentPath = selectionDirectory;
  const saved = readWorkspaceSelection(), url = new URL(location.href);
  const explicit = ['workspace', 'database', 'choose_database'].some(key => url.searchParams.has(key));
  if (!explicit && typeof saved.workspace === 'string' && saved.workspace !== selectionDirectory) {
    // Validate existence and containment at the server before restoring a path.
    const response = await workspaceFetch('/api/manage/info?' + new URLSearchParams({workspace:saved.workspace}));
    const raw = await response.text();
    let target = {};
    try { target = raw ? JSON.parse(raw) : {}; } catch { target = {}; }
    if (response.ok && target.root_id === info.root_id) {
      url.searchParams.set('workspace', saved.workspace);
      location.replace(url.href);
      return true;
    }
    if (![400, 403, 404].includes(response.status)) throw new Error(target.error || '上次目录暂时无法访问');
    rememberWorkspaceSelection();
    notice('上次目录已失效，已返回默认目录');
  }
  return false;
}
function workspaceUrl(value) {
  const url = new URL(value, location.href);
  if (url.origin === location.origin && url.pathname.startsWith('/api/')) {
    if (workspacePath && !url.searchParams.has('workspace')) {
      url.searchParams.set('workspace', workspacePath);
    }
    const bypassSession = /^\/api\/manage\/(efile-sessions|workspaces|database-files|databases|info)(?:\/|$)/.test(url.pathname);
    if (activeEfileSessionId && !bypassSession && !url.searchParams.has('efile_session')) {
      url.searchParams.set('efile_session', activeEfileSessionId);
    }
  }
  return url.href;
}
function workspaceFetch(value, options) { return fetch(workspaceUrl(value), options); }
async function parseJsonResponse(response, fallbackError = '请求失败') {
  const text = await response.text();
  let data = null;
  if (text) {
    try { data = JSON.parse(text); } catch { data = null; }
  }
  if (!response.ok) {
    const message = (data && typeof data === 'object' && data.error) || `${fallbackError} (${response.status})`;
    const error = new Error(message);
    error.status = response.status;
    if (data && typeof data === 'object' && data.details !== undefined) error.details = data.details;
    throw error;
  }
  if (data === null) {
    const error = new Error('服务端返回了非 JSON 响应');
    error.status = response.status;
    throw error;
  }
  return data;
}
function configureWorkspaceScope(info) {
  workspaceScope = info.strict_root ? JSON.stringify([info.root_id, info.data_dir]) : null;
}
function workspaceStorageKey(key) {
  if (workspaceScope) return `${key}:root:${workspaceScope}`;
  return workspacePath ? `${key}:workspace:${workspacePath}` : key;
}

const workspaceTree = {
  nodes: new Map(), children: new Map(), files: new Map(), expanded: new Set(),
  strictRoot: false, currentPath: workspacePath, viewPath: workspacePath, browsingPath: workspacePath,
};

function workspaceParent(path) {
  if (path === '.' || path === '') return null;
  const slash = path.lastIndexOf('/');
  return slash === -1 ? '.' : path.slice(0, slash);
}

function workspaceLabel(path) {
  if (!path) return '默认数据库目录';
  return path === '.' ? '根目录（.）' : path;
}

function workspaceDatabasePath(directory, filename) {
  return directory && directory !== '.' ? directory + '/' + filename : filename;
}

function workspaceEndpoint(endpoint, directory) {
  return endpoint + (endpoint.includes('?') ? '&' : '?') + 'workspace=' + encodeURIComponent(directory);
}

function canSwitchWorkspace() {
  if (backgroundTasks.pending() || databaseUploads.active || csvImports.active || efileImports.active) {
    throw new Error('请等待后台任务完成或暂停上传后再切换目录');
  }
}

function navigateWorkspace(directory, { database, choose } = {}) {
  const url = new URL(location.href);
  url.searchParams.set('workspace', directory);
  if (database) url.searchParams.set('database', database);
  else url.searchParams.delete('database');
  if (choose) url.searchParams.set('choose_database', choose);
  else url.searchParams.delete('choose_database');
  location.assign(url.href);
}

function bindWorkspacePicker() {
  const dialog = el('database-file-dialog');
  const picker = el('workspace-picker');
  const pathInput = el('workspace-path');
  const mkdirName = el('workspace-mkdir-name');

  function positionPicker() {
    const anchor = el('workspace-location').getBoundingClientRect();
    const dialogBounds = dialog.getBoundingClientRect();
    const viewportWidth = document.documentElement.clientWidth;
    const viewportHeight = document.documentElement.clientHeight;
    const margin = 8, gap = 4;
    const top = anchor.bottom + gap;
    const availableHeight = viewportHeight - top - margin;
    if (!anchor.width || anchor.top < dialogBounds.top || anchor.bottom > dialogBounds.bottom || availableHeight <= 0) {
      return false;
    }
    const width = Math.min(anchor.width, viewportWidth - margin * 2);
    picker.style.width = width + 'px';
    picker.style.maxHeight = Math.min(400, availableHeight) + 'px';
    picker.style.left = Math.max(margin, Math.min(anchor.left, viewportWidth - margin - width)) + 'px';
    picker.style.top = top + 'px';
    return true;
  }

  picker.addEventListener('beforetoggle', event => {
    if (event.newState === 'open' && !positionPicker()) event.preventDefault();
  });
  picker.addEventListener('toggle', event => {
    if (event.newState === 'open') loadWorkspaceFileStatus(workspaceTree.viewPath);
  });
  const repositionIfOpen = () => {
    if (picker.matches(':popover-open') && !positionPicker()) picker.hidePopover();
  };
  window.addEventListener('resize', repositionIfOpen);
  dialog.addEventListener('scroll', repositionIfOpen);
  dialog.addEventListener('close', () => picker.hidePopover());

  pathInput.addEventListener('input', () => { el('workspace-path-error').textContent = ''; });
  pathInput.addEventListener('keydown', event => {
    if (event.key === 'Enter' && !event.isComposing) {
      event.preventDefault();
      const errorTarget = picker.matches(':popover-open') ? 'workspace-picker-error' : 'workspace-path-error';
      run(() => openWorkspacePath(pathInput.value.trim()), errorTarget);
    } else if (event.key === 'ArrowDown') {
      event.preventDefault();
      showWorkspacePicker();
    }
  });
  el('workspace-parent').onclick = () => showWorkspaceDirectory(workspaceParent(workspaceTree.browsingPath));
  el('workspace-root').onclick = () => showWorkspaceDirectory('.');
  el('workspace-use-directory').onclick = () => run(async () => {
    canSwitchWorkspace();
    if (!await discardSchema() || !await discardCellEdits()) return;
    navigateWorkspace(workspaceTree.browsingPath, { choose: '1' });
  }, 'workspace-picker-error');
  el('workspace-mkdir').onclick = () => {
    el('workspace-mkdir-fields').hidden = false;
    el('workspace-picker-error').textContent = '';
    mkdirName.value = '';
    mkdirName.focus();
  };
  el('workspace-mkdir-cancel').onclick = () => {
    el('workspace-mkdir-fields').hidden = true;
    el('workspace-picker-error').textContent = '';
    el('workspace-mkdir').focus();
  };
  const createDirectory = () => run(createWorkspaceDirectory, 'workspace-picker-error');
  el('workspace-mkdir-confirm').onclick = createDirectory;
  mkdirName.addEventListener('keydown', event => {
    if (event.key === 'Enter' && !event.isComposing) { event.preventDefault(); createDirectory(); }
  });
  el('workspace-create-database').onclick = () => {
    const form = el('database-file-create');
    updateWorkspaceTarget();
    picker.hidePopover();
    form.hidden = false;
    form.open = true;
    el('database-file-new-name').focus();
  };
}

function updateWorkspaceTarget() {
  const directory = workspaceTree.browsingPath;
  el('workspace-browsing-directory').textContent = '选中目录：' + workspaceLabel(directory);
  el('workspace-create-target').textContent = '新建位置：' + workspaceLabel(directory);
  el('workspace-parent').disabled = workspaceParent(directory) === null;
  el('workspace-root').disabled = !workspaceTree.nodes.has('.') || workspaceTree.viewPath === '.' && directory === '.';
  el('workspace-mkdir').disabled = !workspaceTree.nodes.has(directory) || directory === '';
  el('workspace-use-directory').disabled = !workspaceTree.nodes.has(directory) || directory === workspaceTree.currentPath;
}

async function loadWorkspaceDirectories({ reset = false } = {}) {
  const result = await request('workspaces');
  workspaceTree.strictRoot = result.strict_root === true;
  workspaceTree.currentPath = workspacePath || (workspaceTree.strictRoot ? result.default_data_dir : '');
  workspaceTree.nodes = new Map(result.directories.map(item => [item.path, item]));
  if (!workspaceTree.strictRoot) workspaceTree.nodes.set('', { path: '', files: null });
  workspaceTree.children = new Map();
  for (const item of workspaceTree.nodes.values()) {
    const parent = workspaceParent(item.path);
    if (parent === null) continue;
    if (!workspaceTree.children.has(parent)) workspaceTree.children.set(parent, []);
    workspaceTree.children.get(parent).push(item);
  }

  if (reset || !workspaceTree.nodes.has(workspaceTree.viewPath)) {
    const saved = readWorkspaceSelection().browsing;
    const directory = reset && saved?.workspace === workspaceTree.currentPath && workspaceTree.nodes.has(saved.directory)
      ? saved.directory : workspaceTree.currentPath;
    workspaceTree.viewPath = directory;
    workspaceTree.browsingPath = directory;
    workspaceTree.expanded.clear();
    workspaceTree.files.clear();
    el('workspace-mkdir-fields').hidden = true;
    el('workspace-picker-error').textContent = '';
    el('workspace-path-error').textContent = '';
    el('database-file-create').hidden = true;
    el('database-file-create').open = false;
    const current = state.database ? workspaceDatabasePath(workspaceTree.currentPath, state.database) : '';
    el('workspace-current').textContent = current || '未选择数据库';
    el('workspace-current').title = current;
    el('workspace-path').value = directory === workspaceTree.currentPath ? (current || directory || '.') : directory;
  } else if (!workspaceTree.nodes.has(workspaceTree.browsingPath)) {
    workspaceTree.browsingPath = workspaceTree.viewPath;
    el('workspace-path').value = workspaceTree.viewPath || '.';
  }
  updateWorkspaceTarget();
  renderWorkspaceTree();
}

function showWorkspacePicker() {
  const picker = el('workspace-picker');
  if (!picker.matches(':popover-open')) picker.showPopover();
  loadWorkspaceFileStatus(workspaceTree.viewPath);
}

function showWorkspaceDirectory(directory) {
  if (!workspaceTree.nodes.has(directory)) return;
  workspaceTree.viewPath = directory;
  workspaceTree.expanded.clear();
  selectWorkspaceFolder(directory);
  el('workspace-tree').scrollTop = 0;
  showWorkspacePicker();
}

function selectWorkspaceFolder(directory) {
  workspaceTree.browsingPath = directory;
  rememberWorkspaceFolder(directory);
  el('workspace-path').value = directory || '.';
  el('workspace-path-error').textContent = '';
  el('workspace-picker-error').textContent = '';
  el('workspace-mkdir-fields').hidden = true;
  updateWorkspaceTarget();
  renderWorkspaceTree();
}

async function createWorkspaceDirectory() {
  const name = el('workspace-mkdir-name').value.trim();
  if (!name) throw new Error('目录名称不能为空');
  const result = await request('workspaces/directories', 'POST', { parent: workspaceTree.browsingPath, name });
  await loadWorkspaceDirectories();
  showWorkspaceDirectory(result.path);
}

function refreshWorkspaceFiles(directory) {
  const pending = { files: null, error: null, promise: null };
  workspaceTree.files.set(directory, pending);
  pending.promise = request(workspaceEndpoint('database-files', directory)).then(result => {
    pending.files = result.files || [];
    return pending.files;
  }).catch(error => {
    pending.error = error.message;
    throw error;
  }).finally(() => {
    if (workspaceTree.files.get(directory) === pending) renderWorkspaceTree();
  });
  renderWorkspaceTree();
  return pending.promise;
}

function loadWorkspaceFileStatus(directory) {
  const item = workspaceTree.nodes.get(directory);
  if (item && item.files?.length !== 0 && !workspaceTree.files.has(directory)) {
    void refreshWorkspaceFiles(directory).catch(() => { /* Rendered in the relevant directory. */ });
  }
}

function renderWorkspaceTree() {
  const target = el('workspace-tree');
  const focused = target.contains(document.activeElement) ? document.activeElement.dataset.workspaceControl : null;
  const scrollTop = target.scrollTop;

  function fileRow(file, directory) {
    const relative = workspaceDatabasePath(directory, file.filename);
    const item = node('li');
    const row = node('div', null, 'workspace-tree-row workspace-file-row');
    const button = action('▤  ' + file.filename, async () => {
      if (file.selectable === false) {
        el('workspace-path').value = relative;
        openDatabaseRepair(file, directory);
      } else {
        await run(() => openWorkspaceFile(directory, file.filename), 'workspace-picker-error');
      }
    }, 'workspace-tree-choice workspace-file-name');
    button.title = relative;
    button.dataset.workspaceControl = 'file:' + relative;
    button.disabled = file.selectable === undefined;
    row.append(button);
    if (file.selectable !== undefined) {
      const status = node('span', file.status || (file.selectable ? '可用' : '无法打开'),
                          'workspace-file-status ' + (file.selectable ? 'ok' : 'bad'));
      status.title = file.error || file.status || '';
      row.append(status);
    }
    if (file.selectable === false) {
      const repair = action('处理报警', () => openDatabaseRepair(file, directory), 'secondary small workspace-file-repair');
      repair.setAttribute('aria-label', '处理报警 ' + file.filename);
      repair.dataset.workspaceControl = 'repair:' + relative;
      row.append(repair);
    }
    item.append(row);
    return item;
  }

  function makeList(directory) {
    const list = node('ul', null, 'workspace-tree-list');
    const children = workspaceTree.children.get(directory) || [];
    for (const child of children) {
      const item = node('li');
      const expanded = workspaceTree.expanded.has(child.path);
      const folder = action((expanded ? '▾' : '▸') + '  ' + child.path.split('/').pop(), () => {
        if (expanded) workspaceTree.expanded.delete(child.path);
        else workspaceTree.expanded.add(child.path);
        selectWorkspaceFolder(child.path);
        if (!expanded) loadWorkspaceFileStatus(child.path);
      }, 'workspace-tree-choice workspace-folder');
      folder.title = child.path;
      folder.dataset.workspaceControl = 'folder:' + child.path;
      folder.setAttribute('aria-label', '目录 ' + child.path);
      folder.setAttribute('aria-expanded', String(expanded));
      if (child.path === workspaceTree.browsingPath) folder.classList.add('selected');
      item.append(folder);
      if (expanded) item.append(makeList(child.path));
      list.append(item);
    }

    const listing = workspaceTree.files.get(directory);
    const candidates = workspaceTree.nodes.get(directory)?.files || [];
    const files = listing?.files || candidates.map(filename => ({ filename }));
    for (const file of files) list.append(fileRow(file, directory));
    if (listing?.error) list.append(node('li', listing.error, 'workspace-list-message error-text'));
    else if (listing && !listing.files) list.append(node('li', '正在检查数据库…', 'workspace-list-message muted'));
    else if (!children.length && !files.length) list.append(node('li', '此目录没有子目录或数据库', 'workspace-list-message muted'));
    return list;
  }

  target.replaceChildren(makeList(workspaceTree.viewPath));
  target.scrollTop = scrollTop;
  if (focused) {
    [...target.querySelectorAll('[data-workspace-control]')]
      .find(element => element.dataset.workspaceControl === focused)?.focus({ preventScroll: true });
  }
}

function pathHasInvalidParts(path) {
  return path !== '.' && (!path || /[:\\]/.test(path) || path.split('/').some(part => !part || part.startsWith('.')));
}

async function openWorkspacePath(path) {
  if (pathHasInvalidParts(path)) throw new Error('请输入工作区相对路径，使用 / 分隔');
  if (workspaceTree.nodes.has(path)) {
    showWorkspaceDirectory(path);
    return;
  }
  const slash = path.lastIndexOf('/');
  const directory = slash === -1 ? (workspaceTree.strictRoot ? '.' : '') : path.slice(0, slash);
  await openWorkspaceFile(directory, path.slice(slash + 1));
}

async function openWorkspaceFile(directory, filename) {
  const existing = workspaceTree.files.get(directory);
  const files = existing && !existing.error ? await existing.promise : await refreshWorkspaceFiles(directory);
  const file = files.find(item => item.filename === filename);
  if (!file) throw new Error('找不到数据库：' + workspaceDatabasePath(directory, filename));
  el('workspace-path').value = workspaceDatabasePath(directory, filename);
  if (file.selectable) await selectDatabaseFile(filename, directory);
  else openDatabaseRepair(file, directory);
}
