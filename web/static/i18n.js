/* RTDB 轻量管理台 · 集中式多语言引擎 (zh-CN / en-US / ru-RU) */
(function () {
  const LANG_STORAGE_KEY = "rtdb.ui.lang";
  const SUPPORTED_LANGS = new Set(["zh-CN", "en-US", "ru-RU"]);

  const I18N_DICT = {
    "zh-CN": {
      "app.docTitle": "RTDB 管理台",
      "app.eyebrow": "RTDB WEB SERVICE",
      "app.title": "表结构与数据管理",
      "topbar.langTitle": "切换界面语言",
      "topbar.langAria": "界面语言",
      "topbar.themeTitle": "切换界面视觉风格",
      "topbar.themeAria": "界面风格",
      "topbar.refreshTitle": "刷新",
      "topbar.fullscreen": "全屏显示",
      "topbar.exitFullscreen": "退出全屏",
      "theme.groupLight": "清雅 · 浅色仙境",
      "theme.groupDark": "星穹 · 柔光科幻",
      "theme.minimalist-light": "云岚清辉",
      "theme.arctic-frost": "霜华冰晶",
      "theme.warm-paper": "青玉暖笺",
      "theme.slate-daylight": "雾蓝流光",
      "theme.precision-dark": "星夜柔蓝",
      "theme.ocean-slate": "幽蓝星海",
      "theme.nord-dark": "极夜微光",
      "theme.industrial-amber": "暮霞鎏金",
      "theme.bento-grid": "玉格轻岚",
      "theme.glassmorphism": "星雾琉璃",
      "theme.editorial": "素笺绛霞",
      "theme.sci-fi-cyan": "沧溟仙光",
      "theme.sci-fi-purple": "紫微星穹",
      "theme.sci-fi-matrix": "青莲灵枢",
      "panel.tables": "数据表",
      "panel.data": "表数据",
      "panel.schema": "表结构",
      "btn.newTable": "新建",
      "btn.addRow": "新增行",
      "btn.deleteRows": "删除选中",
      "btn.query": "查询",
      "btn.saveSchema": "保存结构",
      "btn.deleteSchema": "删除结构",
      "btn.cancel": "取消",
      "btn.save": "保存",
      "btn.confirm": "确认",
      "btn.edit": "编辑",
      "btn.close": "关闭",
      "filter.wherePlaceholder": '条件，例如 age >= 18 或 {"status":"启用"}',
      "filter.tableSearchPlaceholder": "搜索表名…",
      "grid.selectAllAria": "全选当前行",
      "filter.whereAria": "筛选条件",
      "filter.orderFieldAria": "排序字段",
      "filter.orderDirAria": "排序方向",
      "filter.defaultOrder": "默认排序",
      "filter.asc": "升序 ASC",
      "filter.desc": "降序 DESC",
      "schema.editorAria": "表结构 JSON",
      "schema.editorPlaceholder": "head + columns JSON 定义",
      "grid.loading": "正在加载…",
      "grid.selectTable": "请选择一张表。",
      "grid.noTables": "暂无数据表，点击“新建”开始。",
      "grid.saveSchemaFirst": "保存结构后即可录入数据。",
      "grid.emptyRows": "当前条件下无数据。",
      "grid.colAction": "操作",
      "grid.rowCount": "{count} 行",
      "grid.summary": "{table} · 共 {count} 条",
      "dialog.addRowTitle": "新增行",
      "dialog.editRowTitle": "编辑行",
      "dialog.rowEditorAria": "行 JSON",
      "dialog.confirmTitle": "确认操作",
      "dialog.deleteSchemaTitle": "删除表结构",
      "dialog.deleteSchemaMsg": "确认删除表 {table} 的定义与数据？此操作不可撤销。",
      "dialog.deleteRowsTitle": "删除选中记录",
      "dialog.deleteRowsMsg": "确认删除表 {table} 中选中的 {count} 条记录？此操作不可撤销。",
      "notice.requestFailed": "请求失败 ({status})",
      "notice.engNameRequired": "head.eng_name 不能为空",
      "notice.schemaSaved": "表结构已保存：{name}",
      "notice.schemaDeleted": "表结构已删除",
      "notice.selectTableFirst": "请先选择或保存一张表",
      "notice.rowSaved": "数据已保存",
      "notice.selectRowsFirst": "请选择要删除的行",
      "notice.rowsDeleted": "已删除 {count} 条记录"
    },
    "en-US": {
      "app.docTitle": "RTDB Console",
      "app.eyebrow": "RTDB WEB SERVICE",
      "app.title": "Schema & Data Manager",
      "topbar.langTitle": "Switch Interface Language",
      "topbar.langAria": "Interface language",
      "topbar.themeTitle": "Switch Visual Theme",
      "topbar.themeAria": "Visual theme",
      "topbar.refreshTitle": "Refresh",
      "topbar.fullscreen": "Fullscreen",
      "topbar.exitFullscreen": "Exit Fullscreen",
      "theme.groupLight": "Ethereal Light",
      "theme.groupDark": "Astral & Soft Sci-Fi",
      "theme.minimalist-light": "Ethereal Mist",
      "theme.arctic-frost": "Frost Crystal",
      "theme.warm-paper": "Jade Parchment",
      "theme.slate-daylight": "Azure Haze",
      "theme.precision-dark": "Starlight Blue",
      "theme.ocean-slate": "Astral Ocean",
      "theme.nord-dark": "Aurora Dusk",
      "theme.industrial-amber": "Twilight Gold",
      "theme.bento-grid": "Soft Bento",
      "theme.glassmorphism": "Nebula Glass",
      "theme.editorial": "Rose Editorial",
      "theme.sci-fi-cyan": "Celestial Cyan",
      "theme.sci-fi-purple": "Astral Violet",
      "theme.sci-fi-matrix": "Lotus Emerald",
      "panel.tables": "Tables",
      "panel.data": "Table Data",
      "panel.schema": "Table Schema",
      "btn.newTable": "New",
      "btn.addRow": "Add Row",
      "btn.deleteRows": "Delete Selected",
      "btn.query": "Query",
      "btn.saveSchema": "Save Schema",
      "btn.deleteSchema": "Delete Schema",
      "btn.cancel": "Cancel",
      "btn.save": "Save",
      "btn.confirm": "Confirm",
      "btn.edit": "Edit",
      "btn.close": "Close",
      "filter.wherePlaceholder": 'Condition, e.g. age >= 18 or {"status":"active"}',
      "filter.tableSearchPlaceholder": "Filter tables…",
      "grid.selectAllAria": "Select all rows",
      "filter.whereAria": "Filter condition",
      "filter.orderFieldAria": "Sort field",
      "filter.orderDirAria": "Sort direction",
      "filter.defaultOrder": "Default Order",
      "filter.asc": "ASC",
      "filter.desc": "DESC",
      "schema.editorAria": "Schema JSON",
      "schema.editorPlaceholder": "head + columns JSON definition",
      "grid.loading": "Loading…",
      "grid.selectTable": "Select a table.",
      "grid.noTables": 'No tables yet. Click "New" to start.',
      "grid.saveSchemaFirst": "Save the schema first to add records.",
      "grid.emptyRows": "No rows match the current filter.",
      "grid.colAction": "Action",
      "grid.rowCount": "{count} rows",
      "grid.summary": "{table} · {count} rows",
      "dialog.addRowTitle": "Add Row",
      "dialog.editRowTitle": "Edit Row",
      "dialog.rowEditorAria": "Row JSON",
      "dialog.confirmTitle": "Confirm Action",
      "dialog.deleteSchemaTitle": "Delete Table Schema",
      "dialog.deleteSchemaMsg": "Delete schema and all data of table {table}? This cannot be undone.",
      "dialog.deleteRowsTitle": "Delete Selected Rows",
      "dialog.deleteRowsMsg": "Delete {count} selected row(s) from table {table}? This cannot be undone.",
      "notice.requestFailed": "Request failed ({status})",
      "notice.engNameRequired": "head.eng_name is required",
      "notice.schemaSaved": "Schema saved: {name}",
      "notice.schemaDeleted": "Schema deleted",
      "notice.selectTableFirst": "Please select or save a table first",
      "notice.rowSaved": "Row saved",
      "notice.selectRowsFirst": "Please select rows to delete",
      "notice.rowsDeleted": "Deleted {count} row(s)"
    },
    "ru-RU": {
      "app.docTitle": "Консоль RTDB",
      "app.eyebrow": "RTDB WEB SERVICE",
      "app.title": "Структура и данные таблиц",
      "topbar.langTitle": "Переключить язык интерфейса",
      "topbar.langAria": "Язык интерфейса",
      "topbar.themeTitle": "Переключить тему оформления",
      "topbar.themeAria": "Тема оформления",
      "topbar.refreshTitle": "Обновить",
      "topbar.fullscreen": "На весь экран",
      "topbar.exitFullscreen": "Выйти из полноэкранного режима",
      "theme.groupLight": "Светлые эфирные",
      "theme.groupDark": "Звёздные и сай-фай",
      "theme.minimalist-light": "Облачный свет",
      "theme.arctic-frost": "Кристальный лёд",
      "theme.warm-paper": "Нефритовый лист",
      "theme.slate-daylight": "Лазурная дымка",
      "theme.precision-dark": "Звёздная синева",
      "theme.ocean-slate": "Астральный океан",
      "theme.nord-dark": "Полярное сияние",
      "theme.industrial-amber": "Сумеречное золото",
      "theme.bento-grid": "Мягкая сетка",
      "theme.glassmorphism": "Туманное стекло",
      "theme.editorial": "Нежная классика",
      "theme.sci-fi-cyan": "Небесный неон",
      "theme.sci-fi-purple": "Звёздный аметист",
      "theme.sci-fi-matrix": "Изумрудный лотос",
      "panel.tables": "Таблицы",
      "panel.data": "Данные таблицы",
      "panel.schema": "Структура таблицы",
      "btn.newTable": "Создать",
      "btn.addRow": "Добавить",
      "btn.deleteRows": "Удалить выбранные",
      "btn.query": "Запрос",
      "btn.saveSchema": "Сохранить схему",
      "btn.deleteSchema": "Удалить схему",
      "btn.cancel": "Отмена",
      "btn.save": "Сохранить",
      "btn.confirm": "Подтвердить",
      "btn.edit": "Изменить",
      "btn.close": "Закрыть",
      "filter.wherePlaceholder": 'Условие, напр. age >= 18 или {"status":"active"}',
      "filter.tableSearchPlaceholder": "Поиск таблиц…",
      "grid.selectAllAria": "Выбрать все строки",
      "filter.whereAria": "Условие фильтра",
      "filter.orderFieldAria": "Поле сортировки",
      "filter.orderDirAria": "Направление сортировки",
      "filter.defaultOrder": "По умолчанию",
      "filter.asc": "По возр. ASC",
      "filter.desc": "По убыв. DESC",
      "schema.editorAria": "JSON схемы таблицы",
      "schema.editorPlaceholder": "JSON-определение head + columns",
      "grid.loading": "Загрузка…",
      "grid.selectTable": "Выберите таблицу.",
      "grid.noTables": "Таблиц пока нет. Нажмите «Создать».",
      "grid.saveSchemaFirst": "Сначала сохраните структуру для ввода данных.",
      "grid.emptyRows": "Нет записей по текущему фильтру.",
      "grid.colAction": "Действия",
      "grid.rowCount": "{count} стр.",
      "grid.summary": "{table} · всего {count}",
      "dialog.addRowTitle": "Новая запись",
      "dialog.editRowTitle": "Редактирование записи",
      "dialog.rowEditorAria": "JSON записи",
      "dialog.confirmTitle": "Подтверждение действия",
      "dialog.deleteSchemaTitle": "Удаление структуры таблицы",
      "dialog.deleteSchemaMsg": "Удалить схему и все данные таблицы {table}? Это действие необратимо.",
      "dialog.deleteRowsTitle": "Удаление выбранных записей",
      "dialog.deleteRowsMsg": "Удалить выбранные записи ({count}) из таблицы {table}? Это действие необратимо.",
      "notice.requestFailed": "Ошибка запроса ({status})",
      "notice.engNameRequired": "Поле head.eng_name не может быть пустым",
      "notice.schemaSaved": "Схема сохранена: {name}",
      "notice.schemaDeleted": "Схема таблицы удалена",
      "notice.selectTableFirst": "Сначала выберите или сохраните таблицу",
      "notice.rowSaved": "Данные сохранены",
      "notice.selectRowsFirst": "Выберите строки для удаления",
      "notice.rowsDeleted": "Удалено записей: {count}"
    }
  };

  let currentLang = "zh-CN";
  const listeners = new Set();

  function normalizeLang(value) {
    if (SUPPORTED_LANGS.has(value)) return value;
    if (typeof value === "string") {
      if (value.toLowerCase().startsWith("en")) return "en-US";
      if (value.toLowerCase().startsWith("ru")) return "ru-RU";
    }
    return "zh-CN";
  }

  function t(key, params = null) {
    const dict = I18N_DICT[currentLang] || I18N_DICT["zh-CN"];
    let text = dict[key] ?? I18N_DICT["zh-CN"][key] ?? key;
    if (params && typeof params === "object") {
      text = text.replace(/\{(\w+)\}/g, (_, k) => (params[k] !== undefined ? String(params[k]) : `{${k}}`));
    }
    return text;
  }

  function applyDom(root = document) {
    document.documentElement.lang = currentLang;
    document.title = t("app.docTitle");
    root.querySelectorAll("[data-i18n]").forEach((node) => {
      node.textContent = t(node.dataset.i18n);
    });
    root.querySelectorAll("[data-i18n-placeholder]").forEach((node) => {
      node.setAttribute("placeholder", t(node.dataset.i18nPlaceholder));
    });
    root.querySelectorAll("[data-i18n-title]").forEach((node) => {
      node.setAttribute("title", t(node.dataset.i18nTitle));
    });
    root.querySelectorAll("[data-i18n-label]").forEach((node) => {
      node.setAttribute("label", t(node.dataset.i18nLabel));
    });
    root.querySelectorAll("[data-i18n-aria-label]").forEach((node) => {
      node.setAttribute("aria-label", t(node.dataset.i18nAriaLabel));
    });
    const select = document.getElementById("lang-select");
    if (select && select.value !== currentLang) select.value = currentLang;
  }

  function setLang(lang, persist = true) {
    currentLang = normalizeLang(lang);
    if (persist) {
      try { localStorage.setItem(LANG_STORAGE_KEY, currentLang); } catch { /* 忽略本地存储异常 */ }
    }
    applyDom(document);
    listeners.forEach((cb) => {
      try { cb(currentLang); } catch { /* 忽略监听器异常 */ }
    });
  }

  try {
    currentLang = normalizeLang(localStorage.getItem(LANG_STORAGE_KEY) || "zh-CN");
  } catch {
    currentLang = "zh-CN";
  }

  window.rtdbI18n = {
    t,
    getLang: () => currentLang,
    setLang,
    apply: applyDom,
    onChange: (cb) => { listeners.add(cb); return () => listeners.delete(cb); },
    dict: I18N_DICT
  };

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", () => applyDom(document), { once: true });
  } else {
    applyDom(document);
  }
})();
