/* RTDB 管理终端 · 运维与诊断中心（高密度单屏收拢库文件/快照、存储维护、后台任务与性能统计） */
window.rtdbOpsCenter = (function () {
  const el = (id) => document.getElementById(id);
  const tr = (key, fallback) => {
    const i18n = window.rtdbI18n;
    if (i18n && typeof i18n.t === "function") {
      const value = i18n.t(key);
      if (value && value !== key) return value;
    }
    return fallback || key;
  };

  function fmtBytes(bytes) {
    if (bytes == null || !Number.isFinite(Number(bytes))) return "—";
    const n = Number(bytes);
    if (n < 1024) return `${n} B`;
    if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KiB`;
    if (n < 1024 * 1024 * 1024) return `${(n / (1024 * 1024)).toFixed(2)} MiB`;
    return `${(n / (1024 * 1024 * 1024)).toFixed(2)} GiB`;
  }

  function fmtTime(iso) {
    if (!iso) return "—";
    const d = new Date(iso);
    return Number.isNaN(d.getTime()) ? String(iso) : d.toLocaleTimeString();
  }

  let host = null;
  let active = false;
  let loading = false;

  function renderDatabases(filesPayload) {
    const tbody = el("ops-db-body");
    const metricDb = el("ops-metric-dbs");
    if (!tbody) return;
    const files = Array.isArray(filesPayload?.files) ? filesPayload.files : [];
    if (metricDb) metricDb.textContent = String(files.length);
    tbody.textContent = "";
    if (!files.length) {
      const trEl = document.createElement("tr");
      const td = document.createElement("td");
      td.colSpan = 5;
      td.className = "st-list-empty";
      td.textContent = "—";
      trEl.appendChild(td);
      tbody.appendChild(trEl);
      return;
    }
    const currentDb = host?.getDatabase ? host.getDatabase() : "";
    for (const item of files) {
      const row = document.createElement("tr");
      if (item.filename === currentDb) row.classList.add("is-focused");

      const tdName = document.createElement("td");
      tdName.className = "st-mono";
      tdName.textContent = item.filename;
      tdName.title = item.path || item.filename;

      const tdSize = document.createElement("td");
      tdSize.className = "st-num";
      tdSize.textContent = fmtBytes(item.size_bytes ?? item.size);

      const tdTables = document.createElement("td");
      tdTables.className = "st-num";
      tdTables.textContent = String(Array.isArray(item.tables) ? item.tables.length : (item.table_count ?? "—"));

      const tdStatus = document.createElement("td");
      const badge = document.createElement("span");
      const isErr = Boolean(item.error);
      const isRo = Boolean(item.read_only);
      badge.className = `ops-pill ${isErr ? "is-danger" : isRo ? "is-warn" : "is-ok"}`;
      badge.textContent = isErr
        ? tr("ops.statusError", "异常")
        : isRo
          ? tr("ops.statusReadOnly", "只读")
          : tr("ops.statusOk", "正常");
      if (item.error) badge.title = item.error;
      tdStatus.appendChild(badge);

      const tdAct = document.createElement("td");
      tdAct.className = "st-col-actions";
      if (item.filename !== currentDb && item.selectable !== false) {
        const btnSel = document.createElement("button");
        btnSel.type = "button";
        btnSel.className = "secondary small";
        btnSel.textContent = tr("ops.btnSwitch", "切换");
        btnSel.onclick = () => host?.selectDatabase?.(item.filename);
        tdAct.appendChild(btnSel);
      }
      if (isErr && host?.openRepair) {
        const btnFix = document.createElement("button");
        btnFix.type = "button";
        btnFix.className = "danger-outline small";
        btnFix.textContent = tr("ops.btnRepair", "修复");
        btnFix.onclick = () => host.openRepair(item);
        tdAct.appendChild(btnFix);
      }
      row.append(tdName, tdSize, tdTables, tdStatus, tdAct);
      tbody.appendChild(row);
    }
  }

  function renderStorageAndReceipts(storagePayload, receiptPayload) {
    const tbody = el("ops-storage-body");
    const metricClean = el("ops-metric-cleanable");
    if (!tbody) return;
    tbody.textContent = "";
    const stats = storagePayload?.statistics || {};
    const cfg = storagePayload?.config || storagePayload?.policy || {};
    const candidates = Array.isArray(storagePayload?.candidates) ? storagePayload.candidates : [];
    const totalCleanBytes = candidates.reduce((sum, c) => sum + Number(c.bytes || 0), 0);
    if (metricClean) {
      metricClean.textContent = candidates.length ? `${candidates.length} (${fmtBytes(totalCleanBytes)})` : "0";
    }

    const rows = [
      {
        name: tr("ops.catUploads", "上传暂存"),
        bytes: stats.uploads?.bytes,
        count: stats.uploads?.files ?? stats.uploads?.count,
        policy: cfg.upload_retention_days ? `${cfg.upload_retention_days}d` : tr("ops.keepAll", "不清理")
      },
      {
        name: tr("ops.catSnapshots", "整库快照"),
        bytes: stats.snapshots?.bytes,
        count: stats.snapshots?.files ?? stats.snapshots?.count,
        policy: cfg.snapshot_retention_days ? `${cfg.snapshot_retention_days}d` : tr("ops.keepAll", "不清理")
      },
      {
        name: tr("ops.catTasks", "任务记录"),
        bytes: stats.tasks?.bytes,
        count: stats.tasks?.records ?? stats.tasks?.count,
        policy: cfg.task_retention_days ? `${cfg.task_retention_days}d` : tr("ops.keepAll", "不清理")
      },
      {
        name: tr("ops.catLogs", "审计日志"),
        bytes: stats.logs?.bytes,
        count: stats.logs?.files ?? stats.logs?.count,
        policy: stats.logs?.retention_days ? `${stats.logs.retention_days}d` : tr("ops.keepAll", "永久")
      }
    ];
    if (receiptPayload && (receiptPayload.pending != null || receiptPayload.archived != null || receiptPayload.delivered != null)) {
      const totalReceipts = Number(receiptPayload.pending || 0) + Number(receiptPayload.delivered || 0) + Number(receiptPayload.archived || 0);
      rows.push({
        name: tr("ops.catReceipts", "写回执归档"),
        bytes: receiptPayload.compressed_bytes ?? receiptPayload.original_bytes,
        count: totalReceipts,
        policy: receiptPayload.days ? `${receiptPayload.days}d` : tr("ops.keepAll", "关闭")
      });
    }

    for (const item of rows) {
      const trEl = document.createElement("tr");
      const tdName = document.createElement("td");
      tdName.textContent = item.name;
      const tdCount = document.createElement("td");
      tdCount.className = "st-num";
      tdCount.textContent = item.count == null ? "—" : String(item.count);
      const tdSize = document.createElement("td");
      tdSize.className = "st-num";
      tdSize.textContent = fmtBytes(item.bytes);
      const tdPol = document.createElement("td");
      tdPol.className = "muted";
      tdPol.textContent = item.policy;
      trEl.append(tdName, tdCount, tdSize, tdPol);
      tbody.appendChild(trEl);
    }
  }

  function renderTasks(tasksPayload) {
    const tbody = el("ops-tasks-body");
    const metricTasks = el("ops-metric-tasks");
    if (!tbody) return;
    const tasks = Array.isArray(tasksPayload?.tasks) ? tasksPayload.tasks : [];
    const activeCount = tasks.filter((item) => item.status === "running" || item.status === "queued").length;
    if (metricTasks) metricTasks.textContent = `${activeCount} / ${tasks.length}`;
    tbody.textContent = "";
    if (!tasks.length) {
      const trEl = document.createElement("tr");
      const td = document.createElement("td");
      td.colSpan = 4;
      td.className = "st-list-empty";
      td.textContent = "—";
      trEl.appendChild(td);
      tbody.appendChild(trEl);
      return;
    }
    for (const task of tasks.slice(0, 15)) {
      const trEl = document.createElement("tr");
      const tdOp = document.createElement("td");
      tdOp.textContent = task.path || task.operation || task.kind || task.id || "—";
      if (task.error || task.phase) tdOp.title = task.error || task.phase;
      const tdDb = document.createElement("td");
      tdDb.className = "st-mono";
      tdDb.textContent = [task.database, task.table].filter(Boolean).join(" · ") || "—";
      const tdStatus = document.createElement("td");
      const pill = document.createElement("span");
      const st = String(task.status || "");
      pill.className = `ops-pill ${st === "done" ? "is-ok" : st === "failed" ? "is-danger" : "is-warn"}`;
      pill.textContent = st || "—";
      tdStatus.appendChild(pill);
      const tdTime = document.createElement("td");
      tdTime.className = "st-num muted";
      const ts = task.updated_at || task.created_at || (task.created ? task.created * 1000 : null);
      tdTime.textContent = fmtTime(ts);
      trEl.append(tdOp, tdDb, tdStatus, tdTime);
      tbody.appendChild(trEl);
    }
  }

  function renderPerf() {
    const tbody = el("ops-perf-body");
    if (!tbody) return;
    const samples = (host?.getPerfSamples ? host.getPerfSamples() : []) || [];
    tbody.textContent = "";
    if (!samples.length) {
      const trEl = document.createElement("tr");
      const td = document.createElement("td");
      td.colSpan = 5;
      td.className = "st-list-empty";
      td.textContent = "—";
      trEl.appendChild(td);
      tbody.appendChild(trEl);
      return;
    }
    for (const s of [...samples].reverse().slice(0, 20)) {
      const trEl = document.createElement("tr");
      const tdTime = document.createElement("td");
      tdTime.className = "st-num muted";
      tdTime.textContent = fmtTime(s.at);
      const tdOp = document.createElement("td");
      tdOp.textContent = `${s.label || "—"} (${s.mode || "full"})`;
      const tdReq = document.createElement("td");
      tdReq.className = "st-num";
      tdReq.textContent = s.request_ms != null ? `${Number(s.request_ms).toFixed(1)} ms` : "—";
      const tdSrv = document.createElement("td");
      tdSrv.className = "st-num";
      tdSrv.textContent = s.server_ms != null ? `${Number(s.server_ms).toFixed(1)} ms` : "—";
      const tdUi = document.createElement("td");
      tdUi.className = "st-num";
      tdUi.textContent = s.ready_ms != null ? `${Number(s.ready_ms).toFixed(1)} ms` : "—";
      if (!s.ok) tdOp.classList.add("st-val-warn");
      trEl.append(tdTime, tdOp, tdReq, tdSrv, tdUi);
      tbody.appendChild(trEl);
    }
  }

  async function refresh() {
    if (!host || loading) return;
    loading = true;
    const errEl = el("ops-error");
    if (errEl) { errEl.textContent = ""; errEl.hidden = true; }
    const dbEl = el("ops-metric-current");
    if (dbEl) dbEl.textContent = host.getDatabase() || "—";
    const sampleEl = el("ops-metric-samples");
    if (sampleEl) sampleEl.textContent = String(host.getSampleCount ? host.getSampleCount() : 0);
    renderPerf();
    try {
      const currentDb = host.getDatabase();
      const [filesRes, storageRes, tasksRes, receiptRes] = await Promise.allSettled([
        host.request("database-files"),
        host.request("storage-maintenance"),
        host.request("tasks"),
        currentDb ? host.request("receipt-maintenance") : Promise.resolve(null)
      ]);
      renderDatabases(filesRes.status === "fulfilled" ? filesRes.value : null);
      renderStorageAndReceipts(
        storageRes.status === "fulfilled" ? storageRes.value : null,
        receiptRes.status === "fulfilled" ? receiptRes.value : null
      );
      renderTasks(tasksRes.status === "fulfilled" ? tasksRes.value : null);
    } catch (error) {
      if (errEl) {
        errEl.textContent = error.message || String(error);
        errEl.hidden = false;
      }
    } finally {
      loading = false;
    }
  }

  function bindEvents() {
    el("ops-refresh")?.addEventListener("click", () => void refresh());
    el("ops-open-files")?.addEventListener("click", () => host?.openFiles?.());
    el("ops-open-snapshots")?.addEventListener("click", () => host?.openSnapshots?.());
    el("ops-open-storage")?.addEventListener("click", () => host?.openStorage?.());
    el("ops-open-receipts")?.addEventListener("click", () => host?.openReceipts?.());
    el("ops-open-tasks")?.addEventListener("click", () => host?.openTasks?.());
    el("ops-open-perf")?.addEventListener("click", () => host?.openPerf?.());
    el("ops-export-perf")?.addEventListener("click", () => host?.exportPerf?.());
    el("ops-metric-samples")?.addEventListener("click", () => host?.openSampleList?.());
  }

  return {
    init(hostApi) {
      host = hostApi;
      bindEvents();
    },
    setActive(flag) {
      active = Boolean(flag);
      if (active) void refresh();
    },
    refresh() {
      if (active) void refresh();
    }
  };
})();
