/* RTDB 管理终端 · 集中式多语言引擎 (zh-CN / en-US / ru-RU)
 * 支持：
 * 1) 显式 data-i18n / data-i18n-title / data-i18n-placeholder / data-i18n-aria-label 绑定；
 * 2) window.rtdbI18n.t(key, params) 动态文案插值；
 * 3) 对 UI 壳层（按钮、标签、弹窗、右键菜单、空状态、汇总）进行无损可逆短语/模板翻译，
 *    严格跳过真实业务数据区域（表数据单元格、表名、字段名、日志正文、SQL 编辑器与用户输入值）。
 */
(function () {
  const LANG_STORAGE_KEY = "rtdb.ui.lang";
  const SUPPORTED_LANGS = new Set(["zh-CN", "en-US", "ru-RU"]);

  // 1. 显式键值字典（核心界面元素与动态消息）
  const I18N_DICT = {
    "zh-CN": {
      "app.docTitle": "RTDB 管理终端",
      "app.title": "RTDB 管理终端",
      "app.titleTip": "双击打开数据库文件管理",
      "app.eyebrow": "实时数据 · SQLite",
      "topbar.toggleSidebar": "折叠 / 展开左侧栏",
      "topbar.openDbFiles": "选择 / 新建数据库",
      "topbar.autoRefresh": "自动刷新间隔",
      "topbar.toggleBottom": "折叠 / 展开底部曲线与日志",
      "topbar.langTitle": "切换界面语言",
      "topbar.langAria": "界面语言",
      "topbar.themeTitle": "切换界面视觉风格",
      "topbar.themeAria": "界面风格",
      "topbar.refresh": "刷新 (F5)",
      "topbar.noDb": "未选择数据库",
      "topbar.connecting": "连接中",
      "theme.minimalist-light": "极简明亮",
      "theme.precision-dark": "精密暗黑",
      "theme.bento-grid": "便当盒网格",
      "theme.glassmorphism": "毛玻璃拟态",
      "theme.editorial": "编辑杂志",
      "theme.sci-fi-cyan": "科幻 · 电光青",
      "theme.sci-fi-purple": "科幻 · 赛博紫",
      "theme.sci-fi-matrix": "科幻 · 矩阵绿",
      "chart.leftAxis": "左轴",
      "chart.rightAxis": "右轴",
      "chart.empty": "右键表格数值选区，选择“左轴曲线”或“右轴曲线”。",
      "chart.allHidden": "全部曲线已隐藏，点击右上角图例恢复显示。",
      "chart.seriesSummary": "已添加 {count} 条曲线{ordinal} · 右键选区继续添加",
      "chart.ordinalHint": " · 横轴：点序号（游标显示各点标签）",
      "cell.invalidMenu": "非法值 ({value})",
      "cell.invalidMenuTip": "非法菜单值：{value} 不在菜单定义范围内",
      "cell.invalidChoiceTip": "非法选择值：{value} 不在选择集合范围内"
    },
    "en-US": {
      "app.docTitle": "RTDB Studio",
      "app.title": "RTDB Studio",
      "app.titleTip": "Double-click to open database file manager",
      "app.eyebrow": "Realtime · SQLite",
      "topbar.toggleSidebar": "Collapse / Expand Sidebar",
      "topbar.openDbFiles": "Select / Create Database",
      "topbar.autoRefresh": "Auto-Refresh Interval",
      "topbar.toggleBottom": "Collapse / Expand Chart & Logs Panel",
      "topbar.langTitle": "Switch Interface Language",
      "topbar.langAria": "Interface language",
      "topbar.themeTitle": "Switch Visual Theme",
      "topbar.themeAria": "Visual theme",
      "topbar.refresh": "Refresh (F5)",
      "topbar.noDb": "No database selected",
      "topbar.connecting": "Connecting",
      "theme.minimalist-light": "Minimalist Light",
      "theme.precision-dark": "Precision Dark",
      "theme.bento-grid": "Bento Grid",
      "theme.glassmorphism": "Glassmorphism",
      "theme.editorial": "Editorial",
      "theme.sci-fi-cyan": "Sci-Fi Cyan",
      "theme.sci-fi-purple": "Sci-Fi Purple",
      "theme.sci-fi-matrix": "Sci-Fi Matrix",
      "chart.leftAxis": "Left Axis",
      "chart.rightAxis": "Right Axis",
      "chart.empty": 'Right-click a numeric range in the grid and pick "Left Axis" or "Right Axis".',
      "chart.allHidden": "All series are hidden. Click legend items at top-right to restore.",
      "chart.seriesSummary": "{count} series pinned{ordinal} · Right-click selection to add more",
      "chart.ordinalHint": " · X: Point Index (hover shows labels)",
      "cell.invalidMenu": "Invalid ({value})",
      "cell.invalidMenuTip": "Invalid menu value: {value} is not defined in menu options",
      "cell.invalidChoiceTip": "Invalid choice value: {value} is not in the allowed set"
    },
    "ru-RU": {
      "app.docTitle": "Терминал RTDB",
      "app.title": "Терминал RTDB",
      "app.titleTip": "Двойной клик — управление файлами БД",
      "app.eyebrow": "Реальное время · SQLite",
      "topbar.toggleSidebar": "Свернуть / развернуть боковую панель",
      "topbar.openDbFiles": "Выбрать / создать БД",
      "topbar.autoRefresh": "Интервал автообновления",
      "topbar.toggleBottom": "Свернуть / развернуть графики и журнал",
      "topbar.langTitle": "Переключить язык интерфейса",
      "topbar.langAria": "Язык интерфейса",
      "topbar.themeTitle": "Переключить тему оформления",
      "topbar.themeAria": "Тема оформления",
      "topbar.refresh": "Обновить (F5)",
      "topbar.noDb": "БД не выбрана",
      "topbar.connecting": "Подключение",
      "theme.minimalist-light": "Минимализм светлый",
      "theme.precision-dark": "Строгий тёмный",
      "theme.bento-grid": "Сетка Bento",
      "theme.glassmorphism": "Глассморфизм",
      "theme.editorial": "Журнальный",
      "theme.sci-fi-cyan": "Сай-фай: Циан",
      "theme.sci-fi-purple": "Сай-фай: Фиолет",
      "theme.sci-fi-matrix": "Сай-фай: Матрица",
      "chart.leftAxis": "Левая ось",
      "chart.rightAxis": "Правая ось",
      "chart.empty": "Щёлкните ПКМ по числовому диапазону и выберите «Левая ось» или «Правая ось».",
      "chart.allHidden": "Все кривые скрыты. Нажмите на легенду справа сверху, чтобы показать.",
      "chart.seriesSummary": "Кривых: {count}{ordinal} · ПКМ по выделению для добавления",
      "chart.ordinalHint": " · Ось X: индекс точки",
      "cell.invalidMenu": "Недопуст. ({value})",
      "cell.invalidMenuTip": "Недопустимое значение: {value} отсутствует в справочнике",
      "cell.invalidChoiceTip": "Недопустимое значение: {value} отсутствует в списке выбора"
    }
  };

  // 2. 全量精确 UI 短语词典（简体中文 -> [英文, 俄文]）
  const PHRASE_MAP = {
    // 顶栏与侧栏
    "关闭": ["Off", "Выкл"],
    "2 秒": ["2s", "2 с"],
    "5 秒": ["5s", "5 с"],
    "10 秒": ["10s", "10 с"],
    "30 秒": ["30s", "30 с"],
    "选择数据库…": ["Select database…", "Выберите БД…"],
    "＋ 新建表": ["+ New Table", "+ Таблица"],
    "搜索表名 (Ctrl+K)": ["Search tables (Ctrl+K)", "Поиск таблиц (Ctrl+K)"],
    "支持英文或中文表名，Ctrl+K 快速聚焦": ["Filter by English or Chinese table name (Ctrl+K)", "Поиск по имени таблицы (Ctrl+K)"],
    "精确匹配": ["Exact", "Точно"],
    "精确": ["Exact", "Точно"],
    "仅非空表": ["Non-empty", "Непустые"],
    "非空": ["Non-empty", "Непуст."],
    "精确匹配表名": ["Exact table name match", "Точное совпадение имени"],
    "仅显示有记录的表": ["Show only tables with rows", "Показать только непустые таблицы"],
    "正在加载…": ["Loading…", "Загрузка…"],
    "显示字段": ["Visible Fields", "Поля"],
    "选择表后查看字段": ["Select a table to inspect fields", "Выберите таблицу для просмотра полей"],

    // 主工作区与 Tab
    "请选择或新建一张表": ["Select or create a table", "Выберите или создайте таблицу"],
    "结果展示": ["Data View", "Данные"],
    "表结构": ["Schema", "Структура"],
    "输入 SQL（Enter 执行，↓ 选择历史）": ["Enter SQL (Enter to run, ↓ for history)", "Введите SQL (Enter — выполнить, ↓ — история)"],
    "历史 SQL": ["SQL History", "История SQL"],
    "快捷筛选…": ["Quick Filter…", "Быстрый фильтр…"],
    "选择字段": ["Select field", "Выберите поле"],
    "比较方式": ["Operator", "Оператор"],
    "包含": ["Contains", "Содержит"],
    "为空": ["Is Null", "Пусто"],
    "非空": ["Not Null", "Не пусто"],
    "值": ["Value", "Значение"],
    "筛选": ["Filter", "Фильтр"],
    "重置": ["Reset", "Сброс"],
    "每页": ["Rows", "На стр."],
    "执行": ["Run", "Выполнить"],
    "执行 SQL（Enter / Ctrl+Enter）": ["Execute SQL (Enter / Ctrl+Enter)", "Выполнить SQL (Enter / Ctrl+Enter)"],
    "中文列名": ["CN Headers", "Имена CN"],
    "数值显示模式": ["Value display mode", "Режим отображения значений"],
    "切换显示原值或显示值": ["Switch between formatted value and raw value", "Переключить форматированное или исходное значение"],
    "显示值": ["Display", "Формат"],
    "原值": ["Raw", "Исходн."],
    "未选择表": ["No Table", "Таблица не выбрана"],
    "新增": ["Add", "Добавить"],
    "批量修改": ["Batch Edit", "Масс. правка"],
    "导入": ["Import", "Импорт"],
    "导出": ["Export", "Экспорт"],
    "删除": ["Delete", "Удалить"],
    "保存改单元格": ["Save Edits", "Сохранить правки"],
    "撤销修改": ["Discard", "Отменить"],
    "从左侧选择一张表": ["Select a table from the left sidebar", "Выберите таблицу на панели слева"],
    "0 条记录": ["0 rows", "0 записей"],
    "方向键移动 · F2/双击编辑 · Delete 清空 · Ctrl+S 保存": ["Arrows move · F2/DblClick edit · Del clear · Ctrl+S save", "Стрелки · F2/2×клик правка · Del очистка · Ctrl+S сохр."],
    "上一页": ["Prev", "Назад"],
    "下一页": ["Next", "Вперёд"],
    "跳转": ["Go", "Перейти"],
    "第": ["Page", "Стр."],
    "页": ["", ""],

    // 表结构页
    "表结构定义": ["Table Schema Definition", "Структура таблицы"],
    "检查数据": ["Validate Data", "Проверить данные"],
    "导出定义": ["Export Schema", "Экспорт схемы"],
    "删除空表结构": ["Drop Empty Table", "Удалить пустую"],
    "保存结构": ["Save Schema", "Сохранить схему"],
    "英文表名": ["Table Name (EN)", "Имя таблицы (EN)"],
    "中文表名": ["Display Name", "Отображаемое имя"],
    "例如 devices": ["e.g. devices", "напр. devices"],
    "例如 设备表": ["e.g. Devices", "напр. Устройства"],
    "＋ 添加字段": ["+ Add Field", "+ Добавить поле"],
    "英文名": ["Field Name", "Имя поля"],
    "中文名": ["Label", "Название"],
    "类型": ["Type", "Тип"],
    "主键": ["PK", "Ключ"],
    "默认值": ["Default", "По умолч."],
    "显示类型": ["Widget", "Виджет"],
    "选项（| 分隔）": ["Options (| separated)", "Варианты (через |)"],
    "选项 / 格式": ["Options / Format", "Опции / Формат"],
    "文本": ["Text", "Текст"],
    "整数": ["Integer", "Целое"],
    "浮点数": ["Float", "Дробное"],
    "布尔": ["Boolean", "Логич."],
    "下拉框": ["Select", "Список"],
    "时间类型": ["Datetime", "Дата/время"],
    "菜单类型": ["Enum Menu", "Справочник"],
    "选择类型": ["Choice Set", "Набор выбора"],
    "菜单选项配置": ["Menu Options", "Варианты меню"],
    "选择选项配置": ["Choice Options", "Варианты выбора"],
    "待选择值": ["Option Value", "Значение"],
    "待选择值（整数）": ["Option Value (Int)", "Значение (целое)"],
    "待选择值（文本）": ["Option Value (String)", "Значение (строка)"],
    "双击配置选择选项": ["Double-click to edit choice set", "Двойной клик — настройка выбора"],
    "暂无待选项，点击“添加选项”新增": ['No options yet. Click "+ Add Option" to add.', "Нет вариантов. Нажмите «+ Добавить»."],
    "＋ 添加选项": ["+ Add Option", "+ Добавить"],
    "Key（整数）": ["Key (Int)", "Ключ (целое)"],
    "Value（显示值）": ["Value (Label)", "Значение (метка)"],
    "操作": ["Action", "Действие"],
    "确定": ["OK", "ОК"],
    "双击配置菜单选项": ["Double-click to edit menu options", "Двойной клик — настройка меню"],
    "暂无菜单项，点击“添加选项”新增": ['No options yet. Click "+ Add Option" to add.', "Нет элементов. Нажмите «+ Добавить»."],
    "修改时间": ["Edit Datetime", "Изменить время"],
    "日期与时间": ["Date & Time", "Дата и время"],
    "毫秒": ["Milliseconds", "Миллисекунды"],
    "存储单位": ["Storage Unit", "Единица хранения"],
    "毫秒 (13位)": ["Milliseconds (13 digits)", "Миллисекунды (13 зн.)"],
    "秒 (10位)": ["Seconds (10 digits)", "Секунды (10 зн.)"],
    "微秒 (16位)": ["Microseconds (16 digits)", "Микросекунды (16 зн.)"],
    "纳秒 (19位)": ["Nanoseconds (19 digits)", "Наносекунды (19 зн.)"],
    "当前时间": ["Now", "Сейчас"],
    "清空 (NULL)": ["Clear (NULL)", "Очистить (NULL)"],
    "显示预览": ["Display Preview", "Предпросмотр"],
    "写入时间戳": ["Stored Timestamp", "Метка времени"],
    "未设置": ["None", "Не задано"],
    "双击或 F2 编辑": ["Double-click or press F2 to edit", "Двойной клик или F2 для правки"],

    // 底部曲线与日志面板
    "曲线": ["Chart", "График"],
    "日志": ["Logs", "Журнал"],
    "未选择修改": ["No selection", "Нет выбора"],
    "清空曲线": ["Clear Chart", "Очистить график"],
    "导出 PNG": ["Export PNG", "Экспорт PNG"],
    "刷新": ["Refresh", "Обновить"],
    "导出当前页": ["Export Page", "Экспорт стр."],
    "曲线方向": ["Orientation", "Ориентация"],
    "按列成曲线": ["Series by Column", "Кривые по столбцам"],
    "按行成曲线": ["Series by Row", "Кривые по строкам"],
    "横轴": ["X Axis", "Ось X"],
    "key / 行序号": ["Key / Row #", "Ключ / № строки"],
    "日期": ["Date", "Дата"],
    "全部日期": ["All Dates", "Все даты"],
    "关键词": ["Keyword", "Поиск"],
    "操作、表名或错误": ["Action, table, or error", "Действие, таблица или ошибка"],
    "类别": ["Category", "Категория"],
    "全部类别": ["All Categories", "Все категории"],
    "结构": ["Schema", "Структура"],
    "数据": ["Data", "Данные"],
    "系统": ["System", "Система"],
    "状态": ["Status", "Статус"],
    "全部状态": ["All Statuses", "Все статусы"],
    "成功": ["Success", "Успешно"],
    "失败": ["Failed", "Ошибка"],
    "查询": ["Query", "Найти"],
    "后台任务": ["Background Tasks", "Фоновые задачи"],
    "尚未加载日志": ["Logs not loaded yet", "Журнал ещё не загружен"],
    "保留天数": ["Retention", "Хранение"],
    "永久": ["Forever", "Бессрочно"],
    "1 天": ["1 day", "1 дн."],
    "3 天": ["3 days", "3 дн."],
    "7 天": ["7 days", "7 дн."],
    "14 天": ["14 days", "14 дн."],
    "30 天": ["30 days", "30 дн."],
    "90 天": ["90 days", "90 дн."],
    "180 天": ["180 days", "180 дн."],
    "365 天": ["365 days", "365 дн."],
    "自定义…": ["Custom…", "Другое…"],
    "保存配置": ["Save Policy", "Сохранить"],
    "立即清理": ["Clean Now", "Очистить"],
    "第 1 / 1 页": ["Page 1 / 1", "Стр. 1 / 1"],
    "正在读取服务信息…": ["Loading service info…", "Загрузка сведений о службе…"],
    "尚未刷新": ["Not refreshed", "Не обновлялось"],

    // 右键菜单（表格 / 曲线 / 表结构 / 侧边栏）
    "复制(CTRL+C)": ["Copy (Ctrl+C)", "Копировать (Ctrl+C)"],
    "粘贴(CTRL+V)": ["Paste (Ctrl+V)", "Вставить (Ctrl+V)"],
    "行操作": ["Row Actions", "Строки"],
    "新增记录": ["Add Row", "Добавить строку"],
    "复制选中行": ["Copy Selected Rows", "Копировать строки"],
    "插入复制行": ["Insert Copied Rows", "Вставить строки"],
    "编辑与草稿": ["Edit & Drafts", "Правки и черновики"],
    "撤销（Ctrl+Z）": ["Undo (Ctrl+Z)", "Отменить (Ctrl+Z)"],
    "重做（Ctrl+Y）": ["Redo (Ctrl+Y)", "Повторить (Ctrl+Y)"],
    "查看待保存修改": ["Review Pending Edits", "Просмотр изменений"],
    "恢复本地草稿": ["Restore Local Draft", "Восстановить черновик"],
    "导入 / 导出": ["Import / Export", "Импорт / Экспорт"],
    "导入 JSON / CSV": ["Import JSON / CSV", "Импорт JSON / CSV"],
    "导出查询（CSV）": ["Export Query (CSV)", "Экспорт (CSV)"],
    "导出查询（JSON）": ["Export Query (JSON)", "Экспорт (JSON)"],
    "绘制曲线": ["Plot Chart", "Построить график"],
    "添加至左轴曲线": ["Plot on Left Axis", "На левую ось"],
    "添加至右轴曲线": ["Plot on Right Axis", "На правую ось"],
    "删除 / 清空": ["Delete / Truncate", "Удалить / Очистить"],
    "删除选中记录": ["Delete Selected Rows", "Удалить выбранные"],
    "清空表": ["Truncate Table", "Очистить таблицу"],
    "坐标轴设置": ["Axis Settings", "Настройки осей"],
    "左轴设置": ["Left Axis Settings", "Левая ось"],
    "右轴设置": ["Right Axis Settings", "Правая ось"],
    "横轴设置": ["X Axis Settings", "Ось X"],
    "样式设置": ["Style Settings", "Стиль графика"],
    "标题设置": ["Title Settings", "Заголовок"],
    "曲线设置": ["Series Settings", "Настройки кривых"],
    "索引设置": ["Index Settings", "Индексы"],
    "预览结构变更": ["Preview Schema Diff", "Предпросмотр схемы"],
    "查看查询计划": ["Explain Query Plan", "План запроса"],
    "删除字段": ["Drop Column", "Удалить столбец"],
    "批量勾选": ["Batch Selection", "Выбор"],
    "全选": ["Select All", "Выбрать все"],
    "反选": ["Invert Selection", "Инвертировать"],
    "取消勾选": ["Clear Selection", "Снять выделение"],
    "数据导入": ["Data Import", "Импорт данных"],
    "加载 dbdef / E": ["Load dbdef / E File", "Загрузить dbdef / E"],
    "CSV 流式导入": ["CSV Streaming Import", "Потоковый импорт CSV"],
    "备份与快照": ["Backup & Snapshots", "Резервные копии"],
    "备份所选表": ["Backup Selected Tables", "Бэкап таблиц"],
    "恢复备份": ["Restore Backup", "Восстановить бэкап"],
    "整库快照 / 恢复": ["Database Snapshots", "Снимки БД"],
    "服务维护": ["Maintenance", "Обслуживание"],
    "日志与回执维护": ["Logs & Receipts", "Журнал и квитанции"],
    "存储维护": ["Storage Maintenance", "Хранилище"],

    // 模态对话框通用按钮与文案
    "取消": ["Cancel", "Отмена"],
    "确认": ["Confirm", "Подтвердить"],
    "保存": ["Save", "Сохранить"],
    "关闭": ["Close", "Закрыть"],
    "应用": ["Apply", "Применить"],
    "创建": ["Create", "Создать"],
    "新建": ["Create", "Создать"],
    "预览": ["Preview", "Просмотр"],
    "暂停": ["Pause", "Пауза"],
    "新增条数": ["Row Count", "Кол-во строк"],
    "1 条": ["1 row", "1 стр."],
    "5 条": ["5 rows", "5 стр."],
    "10 条": ["10 rows", "10 стр."],
    "20 条": ["20 rows", "20 стр."],
    "50 条": ["50 rows", "50 стр."],
    "插入新行": ["Insert Rows", "Вставить строки"],
    "JSON 编辑": ["JSON Mode", "Режим JSON"],
    "选择要修改的字段…": ["Select a field to update…", "Выберите поле…"],
    "添加字段": ["Add Field", "Добавить поле"],
    "导入记录": ["Import Records", "Импорт записей"],
    "选择文件（JSON / CSV，≤16 MiB）": ["Select file (JSON / CSV, ≤16 MiB)", "Файл (JSON / CSV, ≤16 МиБ)"],
    "文本编码": ["Encoding", "Кодировка"],
    "导入方式": ["Import Mode", "Режим импорта"],
    "追加": ["Append", "Добавить"],
    "按主键合并": ["Upsert by PK", "Объединить по PK"],
    "替换整表": ["Replace Table", "Заменить таблицу"],
    "尚未选择文件": ["No file selected", "Файл не выбран"],
    "保存前检查未通过": ["Pre-save Validation Failed", "Ошибка проверки"],
    "应用选择，返回检查": ["Apply & Re-check", "Применить и проверить"],
    "返回修改": ["Back to Edit", "Вернуться к правке"],
    "确认操作": ["Confirm Action", "Подтверждение"],
    "输入名称确认": ["Type name to confirm", "Введите имя для подтверждения"],
    "加载 dbdef / E 文件": ["Load dbdef / E File", "Загрузка dbdef / E"],
    "选择文件": ["Select File", "Выбрать файл"],
    "自动识别": ["Auto-detect", "Авто"],
    "E 数据导入": ["E Data Mode", "Режим импорта E"],
    "追加（冲突跳过）": ["Append (skip conflicts)", "Добавить (пропуск конфл.)"],
    "E 新表主键": ["E Primary Keys", "Первичный ключ E"],
    "暂停上传": ["Pause Upload", "Пауза"],
    "清除临时上传": ["Discard Upload", "Удалить временный"],
    "规则与上限": ["Limits & Rules", "Правила и лимиты"],
    "E 文件上限 128 MiB、每表 10 万条，临时上传保留 24 小时可续传；重复主键追加跳过，合并/替换取末条。": [
      "E file limit: 128 MiB, 100k rows/table; resumable for 24h. Duplicate keys: append skips, merge/replace keeps last.",
      "Лимит E-файла: 128 МиБ, 100 тыс. строк/табл., докачка 24 ч. Дубли PK: добавление пропускает, слияние/замена — последняя."
    ],
    "尚未预览": ["Not previewed yet", "Нет предпросмотра"],
    "确认并生成/更新": ["Apply & Generate", "Применить"],

    // 数据库文件管理 & 修复
    "数据库文件管理": ["Database File Manager", "Файлы баз данных"],
    "服务端文件": ["Server Files", "Файлы сервера"],
    "上传本地文件": ["Upload Local File", "Загрузить локально"],
    "数据库相对路径": ["Relative Database Path", "Относительный путь БД"],
    "输入相对路径后按 Enter": ["Type relative path and press Enter", "Введите путь и нажмите Enter"],
    "上一级": ["Up", "Вверх"],
    "根目录": ["Root", "Корень"],
    "使用此目录": ["Use Folder", "Выбрать папку"],
    "新建目录": ["New Folder", "Новая папка"],
    "新建数据库": ["New DB", "Новая БД"],
    "目录名称": ["Folder name", "Имя папки"],
    "新建服务端数据库": ["Create Server Database", "Создать БД на сервере"],
    "例如 station_a": ["e.g. station_a", "напр. station_a"],
    "例如 A站数据库": ["e.g. Station A DB", "напр. БД станции A"],
    "首张表（可选）": ["Initial Table (optional)", "Первая таблица (опц.)"],
    "留空创建空库": ["Leave empty for blank DB", "Пусто — пустая БД"],
    "选择本地文件（.sqlite3 必选，同名 .json 可选，≤16 MiB）": ["Select local files (.sqlite3 required, matching .json optional, ≤16 MiB)", "Локальные файлы (.sqlite3 обязательно, .json опц., ≤16 МиБ)"],
    "上传整库快照（大文件 / 续传）": ["Upload Full Snapshot (Large / Resumable)", "Загрузить снимок БД (крупный / докачка)"],
    "上传并更新服务端": ["Upload & Activate", "Загрузить на сервер"],
    "处理数据库报警": ["Database Integrity Repair", "Восстановление БД"],
    "1. 重新生成内置定义表": ["1. Rebuild Embedded Definitions", "1. Пересоздать встроенные схемы"],
    "按现有 data 表物理列生成内置结构。": ["Generate embedded definitions from existing physical table columns.", "Создать схемы по физическим столбцам таблиц."],
    "2. 删除未定义数据表": ["2. Drop Undefined Tables", "2. Удалить неописанные таблицы"],
    "先归档到 .web-archive 后删除。": ["Archive to .web-archive before dropping.", "Архивировать в .web-archive перед удалением."],
    "3. 重建数据表": ["3. Rebuild Physical Tables", "3. Перестроить таблицы"],
    "按内置定义重建物理表、索引与元数据。": ["Rebuild physical tables, indexes and metadata from definitions.", "Перестроить таблицы, индексы и метаданные по схемам."],

    // 图表设置弹窗
    "坐标轴标题": ["Axis Title", "Заголовок оси"],
    "例如：电压 / 时间": ["e.g. Voltage / Time", "напр. Напряжение / Время"],
    "最小值": ["Min", "Мин."],
    "最大值": ["Max", "Макс."],
    "自动": ["Auto", "Авто"],
    "标签间隔": ["Tick Step", "Шаг меток"],
    "自动 (1)": ["Auto (1)", "Авто (1)"],
    "每 2 个": ["Every 2", "Каждые 2"],
    "每 5 个": ["Every 5", "Каждые 5"],
    "每 10 个": ["Every 10", "Каждые 10"],
    "每 20 个": ["Every 20", "Каждые 20"],
    "每 50 个": ["Every 50", "Каждые 50"],
    "显示水平网格线": ["Show Horizontal Gridlines", "Горизонтальная сетка"],
    "标题文字": ["Title Text", "Текст заголовка"],
    "留空不显示": ["Leave empty to hide", "Пусто — скрыть"],
    "字体": ["Font", "Шрифт"],
    "默认字体": ["Default", "По умолч."],
    "微软雅黑": ["Microsoft YaHei", "Microsoft YaHei"],
    "宋体": ["SimSun", "SimSun"],
    "黑体": ["SimHei", "SimHei"],
    "字号": ["Size", "Размер"],
    "颜色": ["Color", "Цвет"],
    "样式": ["Style", "Стиль"],
    "常规": ["Normal", "Обычный"],
    "粗体": ["Bold", "Жирный"],
    "斜体": ["Italic", "Курсив"],
    "粗斜体": ["Bold Italic", "Жирн. курсив"],
    "位置": ["Position", "Позиция"],
    "顶部居左": ["Top Left", "Сверху слева"],
    "顶部居中": ["Top Center", "Сверху по центру"],
    "顶部居右": ["Top Right", "Сверху справа"],
    "底部居左": ["Bottom Left", "Снизу слева"],
    "底部居中": ["Bottom Center", "Снизу по центру"],
    "底部居右": ["Bottom Right", "Снизу справа"],
    "间距": ["Gap", "Отступ"],

    // 索引、草稿、性能、任务、维护、快照、上传、CSV
    "辅助索引模式": ["Secondary Index Mode", "Режим индексов"],
    "自动（每列索引）": ["Auto (index every column)", "Авто (по всем столбцам)"],
    "手动配置": ["Custom", "Вручную"],
    "＋ 添加索引": ["+ Add Index", "+ Добавить индекс"],
    "应用到待保存结构": ["Apply to Draft Schema", "Применить к схеме"],
    "结构变更预览": ["Schema Change Preview", "Предпросмотр изменений"],
    "待保存修改": ["Pending Edits", "Несохранённые правки"],
    "继续编辑": ["Keep Editing", "Продолжить правку"],
    "保存全部修改": ["Save All Changes", "Сохранить всё"],
    "性能统计": ["Performance Stats", "Статистика"],
    "导出统计": ["Export Stats", "Экспорт"],
    "执行操作": ["Running Task", "Выполнение задачи"],
    "取消未执行部分": ["Cancel Remaining", "Отменить остаток"],
    "自动压缩周期": ["Auto-Archive Cycle", "Цикл архивации"],
    "立即压缩": ["Archive Now", "Архивировать"],
    "上传暂存保留": ["Upload Temp Retention", "Хранение загрузок"],
    "快照保留": ["Snapshot Retention", "Хранение снимков"],
    "任务详情保留": ["Task Log Retention", "Хранение задач"],
    "不清理": ["Never clean", "Не удалять"],
    "受保护项目": ["Protected Items", "Защищённые объекты"],
    "刷新统计 / 预览": ["Refresh / Preview", "Обновить"],
    "确认清理": ["Confirm Cleanup", "Очистить"],
    "生成当前库快照": ["Create Snapshot", "Создать снимок"],
    "刷新列表": ["Refresh List", "Обновить список"],
    "选择快照后恢复为新数据库": ["Select a snapshot to restore as a new database", "Выберите снимок для восстановления в новую БД"],
    "新数据库英文名": ["New DB Name (EN)", "Имя новой БД (EN)"],
    "中文名称": ["Display Name", "Отображаемое имя"],
    "例如 plant_copy": ["e.g. plant_copy", "напр. plant_copy"],
    "例如 station_copy": ["e.g. station_copy", "напр. station_copy"],
    "留空沿用英文名": ["Leave empty to reuse EN name", "Пусто — использовать EN"],
    "恢复为新库": ["Restore as New DB", "Восстановить БД"],
    "上传整库快照": ["Upload Full Snapshot", "Загрузка снимка БД"],
    "本地 SQLite 快照（≤2 GiB，支持续传）": ["Local SQLite snapshot (≤2 GiB, resumable)", "Локальный снимок SQLite (≤2 ГиБ, докачка)"],
    "可选 SHA-256 校验": ["Optional SHA-256 Checksum", "Проверка SHA-256 (опц.)"],
    "预期 SHA-256": ["Expected SHA-256", "Ожидаемый SHA-256"],
    "可留空": ["Optional", "Необязательно"],
    "上传并校验": ["Upload & Verify", "Загрузить и проверить"],
    "放弃临时上传": ["Discard Upload", "Отменить загрузку"],
    "CSV 文件（≤128 MiB / 10 万条）": ["CSV file (≤128 MiB / 100k rows)", "Файл CSV (≤128 МиБ / 100 тыс. стр.)"],
    "编码": ["Encoding", "Кодировка"],
    "上传并预览": ["Upload & Preview", "Загрузить и просмотреть"],
    "确认导入": ["Confirm Import", "Импортировать"],
    "发现未保存草稿": ["Unsaved Drafts Found", "Найдены черновики"],
    "导出草稿": ["Export Draft", "Экспорт черновика"],
    "删除草稿": ["Delete Draft", "Удалить черновик"],
    "稍后处理": ["Later", "Позже"],
    "恢复草稿": ["Restore Draft", "Восстановить"],

    // 高频动态状态与空状态
    "右键表格数值选区，选择“左轴曲线”或“右轴曲线”。": [
      'Right-click a numeric range in the grid and pick "Left Axis" or "Right Axis".',
      "Щёлкните ПКМ по числовому диапазону и выберите «Левая ось» или «Правая ось»."
    ],
    "全部曲线已隐藏，点击右上角图例恢复显示。": [
      "All series are hidden. Click legend items at top-right to restore.",
      "Все кривые скрыты. Нажмите на легенду справа сверху, чтобы показать."
    ],
    "当前条件下没有记录": ["No matching rows", "Нет записей по условию"],
    "当前 SQL 查询没有记录。": ["No rows returned by current SQL query.", "Текущий SQL-запрос не вернул строк."],
    "当前数据库只读": ["Current database is read-only", "Текущая БД только для чтения"],
    "选项1|选项2": ["Option1|Option2", "Вариант1|Вариант2"],
    "显示名称": ["Display label", "Название"],
    "输入选项文本": ["Enter option text", "Введите текст варианта"],
    "暂无数据表": ["No tables found", "Таблицы не найдены"],
    "表为空": ["Empty table", "Пустая таблица"],
    "已连接": ["Connected", "Подключено"],
    "连接失败": ["Disconnected", "Нет связи"],
    "左轴": ["Left Axis", "Левая ось"],
    "右轴": ["Right Axis", "Правая ось"]
  };

  // 3. 常见带数字的动态文本模板（自动适配 en-US / ru-RU）
  const PATTERN_RULES = [
    {
      re: /^(\d+)\s*\/\s*(\d+)\s*表$/,
      en: (_, a, b) => `${a} / ${b} tables`,
      ru: (_, a, b) => `${a} / ${b} табл.`
    },
    {
      re: /^(\d[\d,]*)\s*字段\s*·\s*共\s*(\d[\d,]*)\s*条$/,
      en: (_, a, b) => `${a} fields · ${b} rows`,
      ru: (_, a, b) => `${a} пол. · всего ${b}`
    },
    {
      re: /^共\s*(\d[\d,]*)\s*条记录$/,
      en: (_, n) => `${n} rows`,
      ru: (_, n) => `Всего: ${n}`
    },
    {
      re: /^(\d[\d,]*)\s*条记录$/,
      en: (_, n) => `${n} rows`,
      ru: (_, n) => `Записей: ${n}`
    },
    {
      re: /^第\s*(\d[\d,]*)\s*\/\s*(\d[\d,]*)\s*页$/,
      en: (_, a, b) => `Page ${a} / ${b}`,
      ru: (_, a, b) => `Стр. ${a} / ${b}`
    },
    {
      re: /^(\d[\d,]*)-(\d[\d,]*)\s*\/\s*(\d[\d,]*)\s*条$/,
      en: (_, a, b, c) => `${a}-${b} / ${c} rows`,
      ru: (_, a, b, c) => `${a}-${b} из ${c}`
    },
    {
      re: /^已刷新\s*(.+)$/,
      en: (_, ts) => `Refreshed ${ts}`,
      ru: (_, ts) => `Обновлено ${ts}`
    },
    {
      re: /^每\s*(\d+)\s*秒自动刷新$/,
      en: (_, s) => `Auto ${s}s`,
      ru: (_, s) => `Авто ${s} с`
    },
    {
      re: /^保存改单元格[（(](\d+)[）)]$/,
      en: (_, n) => `Save Edits (${n})`,
      ru: (_, n) => `Сохранить (${n})`
    },
    {
      re: /^保存修改[（(]新增\s*(\d+)\s*条[）)]$/,
      en: (_, n) => `Save (+${n} new)`,
      ru: (_, n) => `Сохранить (+${n} нов.)`
    },
    {
      re: /^已选\s*(\d[\d,]*)\s*个单元格$/,
      en: (_, n) => `${n} cells selected`,
      ru: (_, n) => `Выбрано ячеек: ${n}`
    },
    {
      re: /^已选\s*(\d[\d,]*)\s*条记录$/,
      en: (_, n) => `${n} rows selected`,
      ru: (_, n) => `Выбрано строк: ${n}`
    },
    {
      re: /^(\d[\d,]*)\s*个单元格已修改$/,
      en: (_, n) => `${n} cells modified`,
      ru: (_, n) => `Изменено ячеек: ${n}`
    },
    {
      re: /^(\d[\d,]*)\s*条新增记录待保存$/,
      en: (_, n) => `${n} new rows pending`,
      ru: (_, n) => `Новых строк: ${n}`
    },
    {
      re: /^(\d[\d,]*)\s*条新增记录待保存\s*·\s*(\d[\d,]*)\s*个单元格已修改$/,
      en: (_, a, b) => `${a} new rows · ${b} cells modified`,
      ru: (_, a, b) => `Новых: ${a} · Изм. ячеек: ${b}`
    },
    {
      re: /^修改时间\s*·\s*(.+)$/,
      en: (_, col) => `Edit Datetime · ${col}`,
      ru: (_, col) => `Изменить время · ${col}`
    },
    {
      re: /^菜单选项配置\s*·\s*(.+)$/,
      en: (_, col) => `Menu Options · ${col}`,
      ru: (_, col) => `Варианты меню · ${col}`
    },
    {
      re: /^选择选项配置\s*·\s*(.+)$/,
      en: (_, col) => `Choice Options · ${col}`,
      ru: (_, col) => `Варианты выбора · ${col}`
    },
    {
      re: /^(.+)（原值:\s*(.+)）$/,
      en: (_, shown, raw) => `${shown} (Raw: ${raw})`,
      ru: (_, shown, raw) => `${shown} (Исх.: ${raw})`
    },
    {
      re: /^已添加\s*(\d+)\s*条曲线(\s*·\s*横轴：点序号（游标显示各点标签）)?\s*·\s*右键选区继续添加$/,
      en: (_, n, ord) => `${n} series pinned${ord ? " · X: Point Index" : ""} · Right-click selection to add`,
      ru: (_, n, ord) => `Кривых: ${n}${ord ? " · X: индекс" : ""} · ПКМ для добавления`
    }
  ];

  // 严格保护真实业务数据区，禁止自动翻译表单元格、表名、字段名、日志正文与用户输入
  const EXCLUDED_DATA_SELECTOR = [
    "#data-grid tbody",
    "#data-grid thead th",
    "#table-list .table-item",
    "#field-list",
    "#logs .log-line",
    "#schema-preview-content",
    "#current-database-file",
    "#workspace-current",
    "#storage-path",
    "#sql-history-list",
    "#edit-preview-grid tbody",
    "#save-conflict-grid tbody",
    "#csv-preview tbody",
    "code",
    "pre",
    "script",
    "style"
  ].join(",");

  let currentLang = "zh-CN";
  let applying = false;
  const listeners = new Set();
  const origTextMap = new WeakMap();
  const origAttrMap = new WeakMap();

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

  function translateText(raw) {
    if (typeof raw !== "string") return raw;
    const trimmed = raw.trim();
    if (!trimmed) return raw;
    if (currentLang === "zh-CN") return raw;
    const lead = raw.slice(0, raw.indexOf(trimmed));
    const tail = raw.slice(raw.indexOf(trimmed) + trimmed.length);
    const pair = PHRASE_MAP[trimmed];
    if (pair) {
      const translated = currentLang === "ru-RU" ? pair[1] : pair[0];
      return `${lead}${translated}${tail}`;
    }
    for (const rule of PATTERN_RULES) {
      const match = trimmed.match(rule.re);
      if (match) {
        const translated = currentLang === "ru-RU" ? rule.ru(...match) : rule.en(...match);
        return `${lead}${translated}${tail}`;
      }
    }
    return raw;
  }

  function isExcludedElement(elem) {
    if (!elem || elem.nodeType !== 1) return false;
    if (elem.id === "lang-select") return true;
    return Boolean(elem.closest(EXCLUDED_DATA_SELECTOR));
  }

  function applyAttrsToElement(elem) {
    if (!elem || elem.nodeType !== 1 || isExcludedElement(elem)) return;
    if (elem.dataset.i18n) {
      elem.textContent = t(elem.dataset.i18n);
    }
    if (elem.dataset.i18nTitle) {
      elem.setAttribute("title", t(elem.dataset.i18nTitle));
    }
    if (elem.dataset.i18nPlaceholder) {
      elem.setAttribute("placeholder", t(elem.dataset.i18nPlaceholder));
    }
    if (elem.dataset.i18nAriaLabel) {
      elem.setAttribute("aria-label", t(elem.dataset.i18nAriaLabel));
    }
    for (const attr of ["placeholder", "title", "aria-label"]) {
      const explicitKey = attr === "placeholder" ? "i18nPlaceholder" : attr === "title" ? "i18nTitle" : "i18nAriaLabel";
      if (elem.dataset[explicitKey]) continue;
      if (!elem.hasAttribute(attr)) continue;
      let saved = origAttrMap.get(elem);
      if (!saved) { saved = {}; origAttrMap.set(elem, saved); }
      const currentVal = elem.getAttribute(attr);
      if (saved[attr] === undefined) {
        saved[attr] = currentVal;
      } else if (currentLang === "zh-CN") {
        if (currentVal !== saved[attr]) {
          // 保留动态更新的新中文值
          if (/[\u4e00-\u9fff]/.test(currentVal || "")) saved[attr] = currentVal;
        }
      } else if (/[\u4e00-\u9fff]/.test(currentVal || "")) {
        saved[attr] = currentVal;
      }
      const nextVal = currentLang === "zh-CN" ? saved[attr] : translateText(saved[attr]);
      if (nextVal !== currentVal) elem.setAttribute(attr, nextVal);
    }
  }

  function applyTextNode(node) {
    const parent = node.parentElement;
    if (!parent || isExcludedElement(parent) || parent.dataset.i18n) return;
    const current = node.nodeValue || "";
    if (!current.trim()) return;
    let orig = origTextMap.get(node);
    if (orig === undefined) {
      orig = current;
      origTextMap.set(node, orig);
    } else if (/[\u4e00-\u9fff]/.test(current) && current !== orig) {
      // JS 动态写入了新的中文文案时更新原文基准
      orig = current;
      origTextMap.set(node, orig);
    }
    const next = currentLang === "zh-CN" ? orig : translateText(orig);
    if (next !== current) node.nodeValue = next;
  }

  function applyDom(root = document.body || document.documentElement) {
    if (!root || applying) return;
    applying = true;
    try {
      document.documentElement.lang = currentLang;
      document.title = t("app.docTitle");
      if (root.nodeType === 1) applyAttrsToElement(root);
      const walker = document.createTreeWalker(root, NodeFilter.SHOW_ELEMENT | NodeFilter.SHOW_TEXT);
      let node;
      while ((node = walker.nextNode())) {
        if (node.nodeType === 1) applyAttrsToElement(node);
        else if (node.nodeType === 3) applyTextNode(node);
      }
      const select = document.getElementById("lang-select");
      if (select && select.value !== currentLang) select.value = currentLang;
    } finally {
      applying = false;
    }
  }

  function setLang(lang, persist = true) {
    currentLang = normalizeLang(lang);
    if (persist) {
      try { localStorage.setItem(LANG_STORAGE_KEY, currentLang); } catch { /* 忽略本地存储异常 */ }
    }
    applyDom(document.body || document.documentElement);
    listeners.forEach((cb) => {
      try { cb(currentLang); } catch { /* 忽略监听器异常 */ }
    });
  }

  try {
    currentLang = normalizeLang(localStorage.getItem(LANG_STORAGE_KEY) || "zh-CN");
  } catch {
    currentLang = "zh-CN";
  }

  let observerScheduled = false;
  const pendingRoots = new Set();
  function scheduleApply(target) {
    if (applying || currentLang === "zh-CN") return;
    pendingRoots.add(target || document.body);
    if (observerScheduled) return;
    observerScheduled = true;
    requestAnimationFrame(() => {
      observerScheduled = false;
      const targets = [...pendingRoots];
      pendingRoots.clear();
      for (const item of targets) {
        if (item && item.isConnected) applyDom(item);
      }
    });
  }

  function startObserver() {
    if (!document.body || typeof MutationObserver !== "function") return;
    const observer = new MutationObserver((mutations) => {
      if (applying || currentLang === "zh-CN") return;
      for (const m of mutations) {
        if (m.type === "characterData") {
          const parent = m.target.parentElement;
          if (parent && !isExcludedElement(parent)) scheduleApply(parent);
        } else if (m.type === "childList") {
          for (const added of m.addedNodes) {
            if (added.nodeType === 1 && !isExcludedElement(added)) scheduleApply(added);
            else if (added.nodeType === 3 && added.parentElement && !isExcludedElement(added.parentElement)) scheduleApply(added.parentElement);
          }
        }
      }
    });
    observer.observe(document.body, { childList: true, subtree: true, characterData: true });
  }

  window.rtdbI18n = {
    t,
    getLang: () => currentLang,
    setLang,
    apply: applyDom,
    translateText,
    onChange: (cb) => { listeners.add(cb); return () => listeners.delete(cb); },
    dict: I18N_DICT
  };

  const boot = () => {
    applyDom(document.body || document.documentElement);
    startObserver();
  };
  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", boot, { once: true });
  } else {
    boot();
  }
})();
