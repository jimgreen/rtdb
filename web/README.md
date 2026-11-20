# SQLite WEB 管理终端

`web/sqlite_server.py` 提供 SQLite HTTP 服务，`web/sqlite_static/` 提供中文 HTML 界面。
SQLite 管理终端的交互参考 Qt `tools/rtdbmng`，包括表索引、字段树、SQL 增删改查、排序、分页和日志区域。
管理对象为本项目 `core.sqlite_db.SQLiteDatabase` 的表：一个 `.sqlite3` 文件可以包含多张逻辑表，
并在文件内的 `rtdb_schema_database`、`rtdb_schema_tables`、`rtdb_schema_columns` 中保存数据库、表和列定义。
JSON/Python 文件作为导入、导出和旧文件首次迁移格式，运行时不再依赖；当前不直接管理任意第三方 SQLite 文件或跨表 JOIN。

页面先选择数据库，再操作表与记录：

1. 在顶部“选择 / 新建数据库”打开服务端文件列表，也可从左侧“当前数据库”下拉框切换已有库。
2. 新建时填写数据库英文名和中文名；“首张表”可留空，先创建空的 `.sqlite3` 文件。
3. 选库后，左侧只列出该库的表。“新建表”与“表结构”页管理表及字段；“结果展示”页通过右键菜单新增、复制、删除记录，双击单元格编辑，`Ctrl+S` 保存，SQL 输入框执行查询或确认后的写入。
4. 不同数据库允许有同名表。切库时先处理未保存修改，并清空当前选择、曲线和编辑历史；SQL 历史按数据库和表隔离。删除最后一张空表后仍保留数据库文件，表改名也保留当前数据库文件名。

未选库时禁用表级操作。dbdef/E 加载、备份恢复也针对当前选择的数据库；结构定义仍以内置表为准，不会因残留 JSON 中有旧表而自动恢复已删除的表。结构修改同时导出该数据库对应的多表 JSON/Python 定义。

结果表格右键提供“撤销上一步”“重做”“查看待保存修改”，也支持 `Ctrl+Z`、`Ctrl+Y`、`Ctrl+Shift+Z`。单元格编辑、批量粘贴、新增草稿和一次连续曲线拖拽分别作为一步；预览展示原值和新值，可点击定位，再保存全部修改。编辑历史限于当前页未提交数据，最多保留 50 步并限制累计编辑项，保存、刷新和切表后清空；数据库中已经执行的删除、SQL 或结构操作不在此撤销范围内。

保存失败时可点击“定位单元格”，定位类型错误、缺失/重复主键等问题。发生记录版本冲突时，窗口展示原值、服务器当前值和本地修改值，逐字段选择“保留本地修改”或“采用服务器值”；服务器已删除的记录需明确选择放弃该行本地修改。应用选择只更新页面待保存内容，并合并服务器其他字段，随后仍需 Ctrl+S 保存，提交时再次检查版本。应用冲突处理选择后，旧撤销历史清空，避免使用旧基线恢复数据。

服务按数据库协调访问：普通数据会话使用共享的文件生命周期锁，允许多个读取及不同数据库访问并行，数据写事务由 SQLite 协调；限定数据库的结构修改只排斥该库会话。整库创建、上传替换等目录级操作仍采用全局排他锁。Windows 使用 LockFileEx 共享/排他字节锁，POSIX 使用 flock；进程间也遵循该边界。[Windows 锁语义](https://learn.microsoft.com/en-us/windows/win32/api/fileapi/nf-fileapi-lockfileex)

选定数据库的 dbdef/E 导入按表提交：同一表的物理结构、内置定义和数据在一个事务中更新，失败整体回滚；结构未变时只修改发生变化的记录，保留未变记录和行位置。多张表仍逐表提交，失败会报告已完成项和未执行项。JSON/Python 是提交后的导出文件；导出失败会明确提示“SQLite 已提交”，并保留恢复日志，由后续访问/服务启动按内置定义重新生成文件。

同页、相同 SQL 和排序、相同字段定义与主键顺序的可编辑结果，刷新时保留行对象和单元格，只更新变化的值，保留选区、滚动位置和已有曲线绑定。翻页、排序、增删记录或字段变化仍完整重建，防止编辑绑定到错误记录；有未保存修改时不自动覆盖。此优化没有改变单页最多 500 行的限制，也不是虚拟滚动。

底部刷新状态显示请求与界面完成耗时，双击可打开“性能统计”，查看服务处理、打开数据库/等锁、SQL/取数、记录解码、版本生成、审计、响应编码、JSON 解析、表格更新、曲线绘制及下一帧估计，并导出 JSON。最近 80 次查询保存在当前页面内存中，不保存 SQL 文本和记录值。服务处理与响应编码取自 `Server-Timing`，分开列出；阶段时间是服务处理的组成部分，不能与总耗时重复相加。界面完成时间包含请求与同步处理；下一帧采用两次动画帧回调，隐藏页面或等待超时时记为空，不代表屏幕实际显示延迟。缓存命中不沿用上一次 SQL 执行时间。

定义文件预览/导入、备份/恢复、结构保存、记录增删改、清空、导入、单元格/新增行批量保存、写入 SQL（INSERT/UPDATE/DELETE）、查询导出和日志查询支持后台执行。普通只读 SQL 仍直接查询。超过快速完成窗口的操作显示阶段、进度和已提交表；没有可靠总量的阶段显示不定进度，不估算虚假的百分比。取消是服务端在安全边界接受的请求：文件校验、日志索引和表间阶段可取消，单表提交或整体结构操作执行期间不能中断。取消不回滚先前已提交的表，结果和日志会列出已完成项。

任务状态和请求指纹存入数据目录 `.web-tasks/ledger.sqlite`，与业务库和审计日志分离。浏览器在提交前记录请求编号；响应丢失或刷新后只查询原任务，不自动重发写入。重复使用同一编号和相同内容返回原状态/结果，编号对应内容不一致则返回 409。详细结果保留 7 天后由启动或后续任务清理，任务状态及去重指纹持续保留；不能通过过期结果重执行写入。同一数据目录只允许一个任务执行服务。日志页“后台任务”可查看最近 30 项跨重启记录。

选定数据库下的 `/api/manage/tables/{name}/rows`、`cell-edits`、`clear`、`import`、写入 `sql` 和 `schema` 新建/修改/删除，把业务修改、结果回执及成功审计事件写入同一个 SQLite 事务，存入内部表 `rtdb_web_commits`，不依赖 JSON/Python 定义文件。即使在提交后、任务状态更新前进程退出，重启也能按同一请求编号与指纹恢复成功结果。没有匹配回执的遗留任务仍标为“中断，需核查”，不推断失败，也不自动重做。选定数据库的多表定义导入及备份恢复还在各表数据提交事务中写入 `rtdb_web_steps`，重启可补充核实已完成表，即使任务进度尚未来得及持久化；其余步骤仍需用户核查，不自动重执行。文件操作和旧 `/api/sqlite` 接口仍按原有事务、日志及中断核查规则处理。内部回执目前持续保留，会占用磁盘；日志保留天数和任务正文保留期不删除这些凭证。

Web 服务对上述事务内审计事件采用后台投递：保存响应返回 `audit_pending=true`，表示业务数据与审计事件已提交，日期日志尚在同步。后台合并约 50 毫秒内的通知，每批按日期追加日志并 fsync，再以一个事务确认投递；失败自动重试，重放按事件编号去重。页面底部显示“日志同步中 / 日志同步待重试”，完成后自动消失。GET `/api/manage/audit-delivery` 可读取待同步数据库和错误；查看日志时最多等待 2 秒同步。正常关闭会尝试排空，强制退出后仍从业务库恢复。独立调用 SQLiteManager 而未启用服务投递器时继续同步写日志。降低保存响应延迟不等于所有日志已经同步完成。

表结构的字段区右键提供“索引设置”“预览结构变更”“查看当前查询计划”。索引设置先修改本地结构草稿，Ctrl+S 才保存；单列和联合索引均支持，联合字段顺序按选择顺序排列。默认继续逐列建立辅助索引；选择手动模式且列表为空时只保留主键唯一索引。不会自动删除现有表的索引。结构定义增加可选 `indexes`，例如 `[["name","value"],["status"]]`，存入 SQLite 内置定义并随多表 JSON/Python 导出；缺省值保持旧索引策略。改字段名会同步调整索引引用，删除被引用字段时须先调整索引配置。索引调整不改变业务值，仅改变索引时不扫描校验全部记录。

结构预览展示增删字段、改名、类型/主键/顺序、字段属性和索引变化；数据迁移校验沿用正式保存逻辑，预览不写业务数据，保存仍再次检查版本。查询计划只分析当前 SQL 输入框中的 SELECT/WITH，使用当前已保存的索引，不执行数据查询或写入。原始计划中的物理字段编号提供对应说明。结构已提交但定义文件导出暂时失败时返回 `export_warning`，页面保持“定义导出待恢复”提示，后续受保护的数据访问或服务启动按内置定义补全导出。

验证命令：`python -m unittest web.test_schema_receipts_indexes`；隔离工作流服务上通过 Playwright CLI 执行 `run-code --filename benchmarks/verify_web_indexes.js`。性能对比：`python benchmarks/benchmark_delivery_indexes.py --output output/delivery-index-performance.json`，报告 HTTP 校验/保存/任务轮询耗时、包含日志同步的耗时，以及两万行宽表的索引策略对照。全部使用临时测试数据；按需索引可能降低其他未覆盖条件的查询性能，应结合实际 SQL 计划和读写基准决定配置。

相关接口使用原管理路由附加 `background=1&request_id={32位小写十六进制编号}`：30 毫秒内完成则直接返回普通结果，否则返回 HTTP 202 和 `task`。随后 GET `/api/manage/tasks/{id}` 查询，POST `/api/manage/tasks/{id}/cancel`（JSON `{}`）申请取消；数据库级任务始终携带同一个 `database` 参数。GET `/api/manage/tasks` 返回最近任务，不携带结果正文。每个服务最多并行 2 个后台任务；每库最多 4 个运行/排队任务，总计最多 16 个。同库后台任务串行，按库轮转，等待同库任务不占执行线程；普通只读 HTTP 查询不进入该队列。已完成任务缓存通常保留 8 个，活跃任务较多时内存上限为 16 个；内存回收不删除持久化回执。排队中的任务可立即取消，执行中的任务继续遵守提交安全边界。

单元格及新增行保存只发一次 `cell-edits` 请求，由该事务内完整检查主键、类型和记录版本后提交；失败整批回滚。独立的 `validate-edits` 预检查接口仍保留给其他调用者。浏览器用 `GET /api/manage/tasks/{id}?wait=2&revision={revision}` 长轮询，任务状态变化立即返回，最多等待 2 秒；不再固定等待 150 毫秒再查询。断线保留请求编号并退避查询，不重发写入。结果过期的重复提交返回原任务及 `result_expired`，不会再执行。长任务窗口的“另页浏览数据库”打开独立页面，原页保持任务上下文，避免切换后完成回调作用到错误数据库。

日志补写每批最多 200 条、约 2 MiB 编码事件（超大单条单独处理），每批后轮转数据库，失败库独立退避 2 秒。启动只先核实中断任务的提交结果，积压日期日志由后台分批恢复，不在启动线程全量重放。`audit-delivery` 另返回正在压缩的数据库和压缩错误。压缩成功/失败及配置修改均记录操作日志。

左侧表列表右键“日志与回执维护”可查看当前库待同步条数、最早积压时间、已压缩条数及明细压缩前后字节数，并设置自动压缩天数（默认 7 天，0=关闭）或按配置立即压缩。`GET/PUT/POST /api/manage/receipt-maintenance?database=...` 分别读取状态、保存 `{ "days": 7 }`、申请压缩；申请成功表示排队，执行结果以维护窗口和日志为准。配置存入当前库 `rtdb_web_receipt_config`。只压缩已同步且过期的回执，默认每批 200 条/约 2 MiB，每小时检查并分批继续处理积压。

归档不是删除：`rtdb_web_receipt_archive` 在同一数据库保存 zlib 压缩的原事件与原结果及 SHA-256 校验值，`rtdb_web_commits` 保留原编号、指纹和归档标记。两者在同一事务更新，恢复及重复请求查到归档仍返回原结果，归档缺失/损坏时拒绝重执行。单独复制完整 SQLite 数据库的备份仍包含这些凭证。此机制降低明细占用，不承诺数据库文件立即缩小，也不限制请求编号、分表完成凭证和任务元数据的长期总量；没有自动 VACUUM 或删除历史凭证。

后台任务记录排队/执行耗时，单元格保存还记录打开/等锁、校验、数据写入、回执写入、事务总耗时。事务总耗时包含内部阶段及提交等待，不可与这些阶段再次相加。页面底部双击性能统计可查看“保存并刷新”的全过程，包含提交响应和随后表格刷新，下一帧仍仅是浏览器帧回调估计。日志全部同步耗时与保存确认耗时分别测量。异步日志标记也会改变 SQLite `data_version`，此时旧分页游标安全回退到普通分页，不改变记录正确性。

本轮验证：`python -m unittest web.test_next_optimization`；隔离页面检查 `benchmarks/verify_next_workflow.js`；对照基准 `python benchmarks/benchmark_next_workflow.py --output output/next-workflow-performance.json`。基准比较同一服务上的旧双请求/固定轮询与新单请求/长轮询流程，任务调度另用受控延迟微基准；全部使用隔离数据库，不将合成调度收益冒充真实 SQL 吞吐提升。

未保存的单元格修改、新增行和表结构编辑以 IndexedDB 草稿保存，按服务目录、数据库、表、页面身份和草稿类别隔离。多个标签页可各自保留同一表的草稿；保存一个页面不删除另一个页面的草稿。结构草稿保留尚未填写完或校验不通过的原始输入。常规写入延迟 250 毫秒合并，不再同步写入大块 localStorage；旧草稿在成功迁移后才删除。离开页面时额外尝试写入不超过 2 Mi 字符的应急副本，大草稿依赖已完成的 IndexedDB 写入。浏览器配额或存储权限失败会明确提示“请勿关闭页面”；恢复期间写入失败也不删除原草稿。本地草稿不能替代主动保存或备份。

刷新后选择该表会提示历史草稿，自动提示排除其他活动页面；结果表右键“恢复本地草稿”可手工选择全部记录/结构草稿，活动页面的草稿禁止直接删除。记录恢复先读取当前记录并比较版本，远方修改/删除进入已有冲突处理；结构版本变化则仅允许导出或删除草稿。恢复本身不写数据库，确认后仍需保存；成功保存仅清除与该请求匹配的草稿版本。

当前页记录数乘以可见列数超过 6000 时，表格启用行、列虚拟渲染，只挂载可视窗口及预留区域的单元格。数据模型仍保留完整当前页，跨窗口选择、复制粘贴、双击编辑、撤销重做、曲线联动和 KEY 冻结继续生效。虚拟模式行高 29 像素、列宽 160 像素；较小表格保持原布局。诊断参数 `?full_grid=1` 可禁用虚拟渲染进行同数据对照，不能代表后端查询加速。

完整表查询按主键顺序连续前后翻页时使用游标；单主键正序/倒序和复合主键默认正序均支持。任意跳页、非主键排序、一般 SQL、数据或结构变化时使用 OFFSET。游标绑定数据库、SQL、排序、页大小和数据版本，服务端最多保留 512 个，15 分钟有效；无效/过期游标自动回退，不用于修改数据。`python benchmarks/benchmark_web_paging.py --output output/paging-performance.json` 在隔离的百万行数据库上比较两种分页，计时包括管理层打开数据库、查询、解码、版本和游标处理，不含 HTTP、审计调度和浏览器。

运行 `python benchmarks/serve_workflow_lab.py` 启动双数据库隔离工作流环境，再在其 URL 下通过 Playwright CLI `run-code --filename benchmarks/verify_web_recovery.js` 验证草稿、版本冲突、游标、任务历史及响应丢失恢复。该脚本会修改测试记录，严禁指向业务数据库。

`benchmarks/verify_web_drafts.js` 验证双页面草稿隔离、保存冲突、结构草稿及配额失败保护；`benchmarks/verify_web_virtual.js` 在渲染测试环境验证虚拟表格交互。`python -m unittest web.test_write_receipts` 验证事务回滚、日志补写去重、重复删除及提交后实际进程退出恢复。`benchmarks/benchmark_web_virtual.js` 比较同一 500×40 页面完整/虚拟绘制，`python benchmarks/benchmark_write_receipts.py --output output/write-receipts-performance.json` 比较管理层写入与日志开销；后者不包含 HTTP 和后台任务账本。性能测试应顺序执行，避免相互争用资源。

前端性能基准见 `benchmarks/benchmark_web_render.js`，通过 Playwright CLI 的 `run-code --filename` 在隔离数据库 `lab.sqlite3/samples` 上运行。该基准会修改并恢复测试行，请勿指向业务数据。URL 带 `?force_full=1` 时仅在该测试页面临时禁用局部更新，可与正常模式在同一后端上做配对对比；原始样本包含 2 次预热和每场景 15 次计时。

运行 `python benchmarks/serve_web_render_lab.py` 可创建并启动上述隔离数据，控制台及 `output/browser-render-lab/server.json` 给出临时端口。需要验证任务进度时加 `--task-logs`，首次建立索引前运行 `benchmarks/verify_web_tasks.js`；`benchmarks/verify_web_render.js` 验证表格局部刷新与曲线/编辑行为。两个检查同样通过 Playwright CLI `run-code --filename` 执行。任务取消检查依赖冷索引，重复运行时应另准备冷索引测试数据。对照 JSON 可通过 `python benchmarks/compare_web_render.py BEFORE.json AFTER.json --output REPORT.html` 生成含全部原始样本的独立 HTML。

从仓库根目录启动：

```powershell
Set-Location D:\codex\rtdb
python -m web.sqlite_server --host 127.0.0.1 --port 8878 `
  --workspace-dir D:\codex\workspace
```

浏览器访问 <http://127.0.0.1:8878>。统一入口同样支持：

```powershell
python -m web.server --engine sqlite --port 8878
```

普通标量字段只用 Python 标准库；`list/array/dict/object/json` 字段需要 `msgpack>=1`。
服务默认仅监听本机。跨站请求被拒绝；本版本没有用户登录或权限角色，
如需局域网访问，应在有身份验证的反向代理后部署。

## 界面操作

- 拖拽左侧竖分割条调整表索引宽度，向左拖到边缘收起；从边缘分割条向右拖拽展开。
- 拖拽查询区下方横分割条调整高度，向上拖到边缘收起；从保留的分割条向下拖拽展开。
- 分区大小和收起状态自动记忆。分割条聚焦后也可用方向键微调，Home 收起、End 展开至上限。
- 查询、记录操作和分页保持单行；窄窗口可横向滚动工具栏。表格占满剩余高度并独立滚动。
- 数据路径和刷新状态固定在页面底部单行显示；结果、表结构、曲线和日志区域自动填满剩余高度，曲线随可用空间重新绘制。
- 左侧按英文/中文表名搜索，可只显示非空表，查看字段和联合主键。表索引保持两行：第一行显示表数量和新建按钮，第二行显示搜索框和筛选项；拖窄侧栏后筛选文字缩短，不隐藏操作入口。
- 表格支持方向键移动、Shift+方向键扩选、Ctrl+A 全选当前页、Home/End 移到行首/行尾（加 Ctrl 移到首行/末行）；Enter/Tab 确认当前编辑并移动。Ctrl+S 保存当前数据修改或表结构，仍执行原有主键、类型和并发版本检查。
- 普通单元格仍需双击编辑；可编辑的 BOOL 单元格使用单击复选框，改动高亮、保存后才写库。操作结果以不占布局高度的浮层提示，失败信息保留到关闭；处理时顶部显示进度条。详细结果继续存入服务端日志。
- 自动刷新检测到当前页数据和查询条件未变化时保留表格节点、选区、焦点和滚动位置；发生变化才更新。表格选区和曲线游标按动画帧合并绘制，单元格联动直接定位目标，游标移动不再重建图例；大量曲线点自动疏化横轴标签。
- 表结构页支持编辑英文名、中文名、类型、KEY、显示类型、默认值和选项，并可配置辅助索引。手工结构迁移会校验已有记录的类型转换、主键唯一性等条件；失败整体回滚。dbdef/E 导入仍遵循已有数据时的保守结构限制。支持完整性检查、导出定义和删除空表。
- 结果页顶部以单行输入框统一输入 SELECT / WITH / INSERT / UPDATE / DELETE，点击“执行”或按 Enter / Ctrl+Enter；历史语句通过输入框内的下拉列表选择，不再设独立模板、历史、重置控件或 SQL 页面。
- SELECT 结果直接显示在主表格，支持投影、别名、聚合、50/100/200/500 行分页和点击列名排序；SQL 自带的 LIMIT/OFFSET 会先应用，再对其结果分页。
- “重置”恢复当前表的 `SELECT *` 原始记录视图，可勾选、编辑、复制和批量操作。自定义查询结果通过 SQL 修改，避免将计算列或聚合行误作原始记录。
- 写入确认后显示影响行数并切回当前表查询；刷新、自动刷新、翻页和导出仅使用上一次成功查询，不执行输入框中尚未提交的语句。
- 新增记录先输入条数（默认 1 条），再在表格中填写空白行；先“复制选中行”，再在目标位置“插入复制行”，生成待保存的副本。记录编辑窗口仍支持字段表单和 JSON 模式。
- 编辑/批量删除使用完整主键和记录摘要；其他连接修改后的旧版本请求返回 409，整批删除回滚。
- 导入 CSV/JSON 采用一次事务，失败不留下部分记录；导出包含上一次成功 SQL 查询的全部结果及排序，最多 100,000 行，CSV 遵循显示字段选择。
- 清空表、删除表和 SQL 写入需要输入表名确认；SQL 写入使用执行时最新内容，不带行版本检查。
- 日志页读取服务端持久化审计日志，记录结构、数据和系统操作的成功/失败、HTTP 状态、耗时、结果摘要和错误信息；新日志按本地日期写入数据目录的 `rtdb-operation-logs/YYYY-MM-DD.jsonl`，旧版单文件日志继续兼容读取。页面支持指定日期、关键词、类别、状态过滤和分页；导出当前查询页。日志保留天数可在页面设置，`0` 表示永久保留；设置大于 0 后，服务启动及每天首次写入时自动清理过期日期文件，也可立即执行清理。
- 双击顶部“RTDB 管理终端”标题也可打开数据库文件管理窗口。服务端文件页按文件名、表名列表、记录数、定义文件和校验状态浏览 `.sqlite3` 文件，可打开有效文件或新建空库及带首张表的数据库；上传本地文件页支持同名 `.sqlite3` 与可选 `.json` 定义。带 SQLite 内置结构的文件无需 JSON 即可加载、校验和上传；没有内置结构的旧文件才使用同名 JSON 首次迁移。已有文件必须确认覆盖，失败不会替换原文件，相关成功/失败均写入操作日志。

表定义统一为：

```json
{
  "head": {"chn_name": "设备", "eng_name": "devices"},
  "columns": [
    {"chn_name": "站点", "eng_name": "site", "primary_key": true, "data_type": "str", "disp_type": "文本", "disp_menu": ""},
    {"chn_name": "编号", "eng_name": "id", "primary_key": true, "data_type": "int", "disp_type": "数字", "disp_menu": ""},
    {"chn_name": "状态", "eng_name": "state", "primary_key": false, "data_type": "str", "disp_type": "下拉框", "disp_menu": "启用|停用"}
  ]
}
```

一个数据库文件可以改用 grouped 定义，同时描述多张表：

```json
{
  "database": {"eng_name": "ems", "chn_name": "EMS 数据库"},
  "tables": [
    {"head": {"chn_name": "节点表", "eng_name": "nodes"}, "columns": []},
    {"head": {"chn_name": "线路表", "eng_name": "lines"}, "columns": []}
  ]
}
```

`tables` 中每张表仍使用完整的 `head` 和 `columns` 定义；空数组表示空数据库。SQLite 内部除上述三张结构表外，为每张逻辑表创建独立的 `rtdb_data_<表名>` 和 `rtdb_metadata_<表名>`，因此表级 CRUD、结构指纹和记录统计互不覆盖。界面加载包含多个模块的 dbdef/E 文件时，各模块作为当前库中的表创建或更新。旧 API 未指定数据库时，仍默认按输入文件名生成一组同名 `.sqlite3`、`.json` 和 `.py` 文件；API 未提供源文件名时使用第一张表的英文名，也可通过 `database_name` 指定数据库文件名。删除 JSON/Python 后，SQLite 文件仍可独立打开、编辑和继续进行结构操作；结构修改时会按需重新生成外部定义文件。

文件映射、Redis、SQLite 三个版本都不再接受 `max_elements`、`data_length`。
SQLite/Redis 没有定义级容量或长度限制。文件映射仍由引擎参数控制物理布局，详见
[公共定义说明](../def/table/README.md)。旧 SQLite/Redis 结构指纹需要离线迁移，
不会自动改写已有数据库；新建库直接使用新格式。

## SQLite API

`GET /api/manage/databases` 返回数据库目录（文件名、中文名、表数和异常信息），仅读取定义，不对每个库执行完整扫描。选择数据库是浏览器自身状态，不改变服务端全局当前库。所有表级接口及定义导入、备份恢复带 `?database=plant_a.sqlite3`，例如 `GET /api/manage/tables?database=plant_a.sqlite3`。文件名限定在服务配置的数据目录内；未指定数据库而存在同名表时拒绝歧义访问。审计操作路径同时记录数据库参数。

SQLite Web 使用一个文件根目录。默认是仓库根目录的 `../workspace`，本机对应 `D:\codex\workspace`。在“选择 / 新建数据库”中，输入数据库相对路径后按 Enter，或在浮层中展开目录并点选数据库。文件状态和“处理报警”入口在同一列表中显示。“上一级”和“工作区根目录”用于导航，“新建目录”和“新建数据库”作用于选中的目录。浏览目录不切换当前库。切换数据库会检查未保存编辑，各目录的数据库、任务、上传会话和草稿互相隔离。

启动参数 `--workspace-dir` 可指定已有的绝对目录，也可用相对于 `--root` 的路径。默认数据目录为 `.`，定义目录为数据目录下的 `.rtdb-schema`。可用 `--data-dir` 和 `--schema-dir` 配置根目录内的其他位置，相对参数均从文件根目录解析。API 返回的数据和文件路径也以根目录为基准。目录请求只接受使用 `/` 分隔的相对路径，拒绝绝对路径、父级跳转、符号链接和 Windows 联接点。扫描跳过隐藏目录及运行日志。数据库、定义、任务、上传和快照均使用根目录内的目录。

例如 `http://127.0.0.1:8877/?workspace=realtime/qinling/sqldb/model&database=devices.sqlite3`。数据库文件名与目录在 URL 中分别指定，不同目录可以使用相同文件名。同一浏览器会记住每个文件根目录上次使用的位置和各目录最后打开的数据库。直接打开首页时自动恢复，链接中的目录和数据库优先。API `GET /api/manage/workspaces` 返回根目录（`path: "."`）及各级子目录。验证命令为 `python -m unittest web.test_workspace`。

带 RTDB 内置定义的原生 SQLite 库（`storage_format=native`）可以在 Web 中查询、增删改记录，也可以新建表、修改表名和显示信息、增加或重命名字段，以及删除不受约束引用的字段和空表。结构变更使用 SQLite 的原生 DDL，物理表和内置定义在同一事务提交。已有字段的类型、默认值、主键及原有索引由原表定义。PAS、SCADA 等应用仍直接使用原表，调整它们依赖的表名和字段前应先核对应用配置。

`POST /api/manage/database-files/create` 可传 `{"name":"plant_a","chn_name":"A站","tables":[]}` 创建空库。Python 调用可用 `SQLiteDatabase(data_dir, schema_directory=schema_dir, database_file="plant_a.sqlite3")` 定位同名表；此限定库实例当前使用直接事务，不支持 `write_process()`。

网页查询发送 `compact: true`：可编辑结果只返回 `records`，避免 `rows` 重复传输；只读结果只返回 `rows`。自动刷新附带上次的 `result_version`（请求字段 `if_version`），无变化返回 `unchanged: true`；可编辑结果再附带 `known_records` 时，变化响应只带新增/改变的记录与完整 `record_order`，客户端据此合并及移除已删除记录。无变化时保留现有表格 DOM。原有未开启 compact 的 API 和导出格式保持不变。

限定数据库的 `SELECT * FROM 表名` 支持在执行 SQL 前检查 `PRAGMA data_version`：同一只读观察连接检测其他连接（包括外部进程）的提交，版本与查询条件均未变时返回 `cache_hit: true`，跳过记录读取与摘要计算。最多保留 16 个观察连接、每库 32 项查询元数据；淘汰、文件替换、结构变更后重新建立观察基线，查询期间发生提交时不缓存该结果。一般 SQL（包括时间、随机数和计算表达式）不走此缓存。[SQLite data_version 语义](https://www.sqlite.org/pragma.html#pragma_data_version)

服务层性能基准：`python benchmarks/benchmark_web_workflow.py --output output/workflow-timing.json`，默认使用临时目录、两个各 10,000 行的数据库、每页 500 行和 30 次重复；报告完整查询、无变化刷新，以及另一数据库持有 150 ms 会话时的响应耗时。该基准不含 HTTP、浏览器绘制和审计日志 fsync，不能直接作为端到端响应时间。

日志查询使用 `rtdb-operation-logs/.query-index.sqlite3` 作为可重建索引，日期 JSONL 文件仍是原始记录，写入时继续 flush/fsync。首次查询建立相关文件的索引；以后只解析追加内容，分页和过滤从索引读取。重启后复用索引；文件替换、截断、删除后重新同步，保留期清理同时移除索引中的过期记录。指定日期仅索引该日期及旧版单文件日志；读取日期列表不扫描各日期文件内容。关键词和操作名称保持 Unicode 大小写折叠及字面子串匹配（`%`、`_` 不作为通配符），记录顺序和分页总数保持原规则。索引构建不占用日志追加锁；索引损坏会重建，索引不可用时回退原始文件读取。索引会额外占用磁盘空间，首次建索引耗时应与后续查询分开评估。

批量保存先在同一事务内分批读取所需主键记录（每次最多 900 个绑定参数），再校验版本、主键重复和字段值；复合主键也支持批量查询。所有检查通过后才执行写入，任一条冲突仍拒绝整批保存。该优化不放宽既有主键、类型或并发冲突约束。

日志与批量保存基准：`python benchmarks/benchmark_web_history.py --output output/history-timing.json --rows 100000 --repeats 15`。默认生成五个日期、共 10 万条日志及一张 1 万行测试表，报告首次查询、分页、日期过滤、关键词过滤、重新打开和保存 500 行的中位/P95 耗时；全部使用临时目录，不操作业务库。结果包含查询内容摘要，可核对优化前后分页结果、总数和日期列表是否一致。这里包含文件读取和 SQLite 提交，不包含 HTTP、浏览器渲染及 API 审计日志追加。

网页统一 SQL 入口使用 `POST /api/manage/tables/{name}/sql`：传入 `sql`、`limit`、`offset`，
可带 `order: {column, direction}`。查询返回 `columns`、`rows`、`total`；原始记录视图另带用于编辑的 `records` 引用。
刷新/翻页传 `readonly_only: true`，服务器拒绝写入语句。写入传 `confirm` 表名并返回 `rowcount`。
`POST /api/manage/tables/{name}/sql-export` 使用相同查询和排序，另传 `format`（JSON/CSV）及 CSV `columns`。
以下 `/api/sqlite/` 接口仍保留，其中原生 SQL 接口的单次返回上限为 500 行。

| 方法 | 路径 | 用途 |
| --- | --- | --- |
| GET | `/api/health`、`/api/sqlite/info` | 服务、路径、SQLite 版本、持久化配置 |
| GET | `/api/manage/logs?page=1&page_size=50&date=YYYY-MM-DD&keyword=...&category=...&status=...` | 按日期、关键词、类别和状态分页查询持久化操作日志 |
| GET | `/api/manage/database-files` | 浏览服务端 RTDB `.sqlite3` 文件及定义、记录数、大小和校验状态 |
| POST | `/api/manage/database-files/select` | 选择并校验服务端文件，参数为 `{ "filename": "devices.sqlite3" }` |
| POST | `/api/manage/database-files/create` | 新建服务端数据库文件；单表参数为 `{ "name": "measurements", "chn_name": "测量表" }`，多表可增加 `tables: [{"head": ..., "columns": ...}, ...]` |
| POST | `/api/manage/database-files/upload` | 上传并原子更新文件，参数为 Base64 `files: [{ "name": "x.sqlite3", "content": "..." }, { "name": "x.json", "content": "..." }]`；覆盖时增加 `confirm` |
| POST | `/api/manage/database-files/rebuild-embedded-definition` | 根据旧 `data` 表的 `cN` 字段生成 SQLite 内置定义，并归档操作前文件 |
| POST | `/api/manage/database-files/delete-orphan` | 删除未定义数据表；必须携带 `{ "filename": "x.sqlite3", "confirm": "x.sqlite3" }`，文件会先归档 |
| POST | `/api/manage/database-files/rebuild-from-embedded` | 根据 SQLite 内置定义重建物理数据表、索引和元数据 |
| GET / PUT | `/api/manage/log-config` | 读取/设置日志保留天数；`0` 表示永久保留 |
| POST | `/api/manage/logs/cleanup` | 按当前保留天数立即清理旧日志文件 |
| GET | `/api/sqlite/tables` | 所有定义、结构版本和记录数 |
| POST | `/api/sqlite/schemas` | 用 `{ "definition": {...} }` 新建表 |
| GET / PUT | `/api/sqlite/tables/{name}/schema` | 读取/修改定义；PUT 携带 `definition` 和 `version` |
| GET | `/api/sqlite/tables/{name}/rows` | `where`、`order`、`limit`、`offset` 查询 |
| POST | `/api/sqlite/tables/{name}/rows` | `{ "rows": [...] }` 原子批量追加 |
| PUT | `/api/sqlite/tables/{name}/row` | `{ "key": [...], "values": {...}, "version": "..." }` |
| DELETE | `/api/sqlite/tables/{name}/rows` | `{ "records": [{"key": [...], "version": "..."}] }` |
| POST | `/api/sqlite/tables/{name}/clear` | `{ "confirm": "表名", "expected_count": 数量 }` |
| DELETE | `/api/sqlite/tables/{name}/table` | `{ "confirm": "表名", "version": "结构版本" }`，仅空表 |
| POST | `/api/sqlite/sql` | `{ "table": "表名", "sql": "...", "confirm": "写入时的表名" }` |
| POST | `/api/sqlite/tables/{name}/import` | `{ "format": "csv或json", "text": "内容" }` |
| GET | `/api/sqlite/tables/{name}/export` | `format=csv/json`，可带 `where`、`order` |
| GET | `/api/sqlite/tables/{name}/check` | quick_check、实际数量与元数据数量核对 |

`POST /api/manage/definition-preview` 与 `POST /api/manage/definition-import` 接受可选的 `source_name` 或 `database_name`。一次请求包含多张表时，所有表始终写入同一个 grouped `.sqlite3`、`.json` 和 `.py` 文件；未提供数据库名时，使用请求中第一张表的英文名作为文件名。

SELECT/WITH 查询和单条 INSERT/UPDATE/DELETE 使用 SQLite 原生执行。
支持聚合、表达式、LIKE、IN 等；DML 通过受限可写视图触发器做类型和主键校验。
PRAGMA、ATTACH、DDL、扩展加载、修改内部表和多语句执行被拒绝；写入 RETURNING、INSERT/UPDATE OR 冲突策略暂不支持。
表名 data、metadata、sqlite_ 前缀由内核保留。
结构化对象写入请用记录/导入 API。SQL 查询最多返回 500 行，执行时限 5 秒。
分页和导入导出的接口限制用于控制单次请求资源：单页 500、导入导出 100000 行、请求体 32 MiB，
这些不是数据库总行数限制。

CSV 第一行是英文字段名，UTF-8 可带 BOM；NULL 使用 `\N`，以反斜杠开头的文本需转义。
JSON 导入可用记录数组，或包含 `rows` 数组的导出对象。
浏览器返回的大整数转为十进制字符串，以保留 64 位主键精度；整数列接受该字符串作为输入。

SQLite 保持 WAL + synchronous=FULL。每次请求创建并关闭自己的连接，提交后其他已打开连接
下一次读取可见新数据；网页通过查询/刷新获取变化，没有 WebSocket 自动推送。
当前服务的管理操作使用进程内及跨进程锁串行协调结构操作，批量导入使用单次事务，
尚未接入 SQLiteWriteProcess 的跨 HTTP 请求分组提交。

已初始化表的打开只校验内置定义与物理结构指纹，不重复写入定义表；首次建表和旧库
首次迁移仍使用写事务。表列表在同一请求内复用已加载的定义，下一次请求重新加载，
避免跨请求缓存过期。无过滤分页及完整表 SQL 查询复用事务内的记录总数；过滤分页
单独统计匹配数量，不为计数排序。数量与当前页数据保持在同一读取快照内。

有数据时允许修改中文名/显示信息，以及增删非主键字段；新增字段的原有记录值为 NULL，删除字段会删除对应数据。
已有字段类型、主键（包括增删主键字段）和保留字段的相对顺序不可改；新字段可插入任意位置。
表单锁定已有字段英文名以保留字段身份，新增字段可选择类型。空表可以调整类型、主键和顺序。
结构保存通过单个 SQLite 事务迁移，保留记录位置、计数和自增序列，重新建立索引；先更新 SQLite 内置结构，再按需同步更新 JSON/Python 导出定义。
结构操作前须关闭其他程序持有的表对象，完成后重新打开；这是结构变更的要求，日常数据写入无需如此。
结构发布使用事务、临时文件 fsync/replace 及恢复日志；失败后按 SQLite 指纹恢复定义。
删除空表会把数据库和定义移入 `.web-archive`；一个 schema 目录应只绑定同一数据目录。

回归命令：

```powershell
python -m unittest web.test_server web.test_sqlite_server web.test_management `
  web.test_definition_import web.test_schema_migration core.test_table_schema `
  core.test_sqlite_db core.test_sqlite_writer core.test_file_mapped_db `
  core.test_file_mapped_writer core.test_redis_db -q
```

---

# RTDB WEB 服务

`web/server.py` 提供一个只使用 Python 标准库的线程化 HTTP 服务，默认使用 SQLite 内核，绑定：

```text
http://127.0.0.1:8877
```

从仓库根目录启动：

```powershell
python -m web.server --engine sqlite
```

也可以指定文件根目录和其中的默认数据目录：

```powershell
python -m web.server --engine sqlite --host 127.0.0.1 --port 8877 `
  --workspace-dir D:\codex\workspace `
  --data-dir realtime/qinling/sqldb/model
```

打开根地址即可使用浏览器管理页面。页面参考 `tools/rtdbmng` 提供表索引过滤、字段显示选择、SQL 增删改查、排序分页、记录增删改复制、批量修改、CSV/JSON 导入导出、多表备份恢复、数据检查、曲线和操作日志。

SQLite 管理台使用 `WAL + synchronous=FULL`。网页 API 使用逻辑主键和行版本校验，排序、分页后编辑不会依赖行号；批量修改、删除和清空遇到并发变化会整体撤销。恢复备份先校验所有表结构、主键和数据，再逐表提交并报告已完成项目。

默认服务入口等价于 `python -m web.sqlite_server --port 8877`。如需继续使用旧的文件映射页面，可明确指定 `--engine file_mapped`；该模式仍使用 `/api/schemas`、`/api/data/...` 兼容接口，默认页面保留在 `web/static/`。

## 表结构接口

表结构 JSON 使用 `head` 和 `columns` 两部分，字段固定为：

```text
chn_name, eng_name,
primary_key, data_type, disp_type, disp_menu
```

接口：

| 方法 | 路径 | 作用 |
| --- | --- | --- |
| GET | `/api/schemas` | 列出全部表结构 |
| GET | `/api/schemas/{table}` | 查询一张表结构 |
| POST | `/api/schemas` | 新增表结构，表名取 `head.eng_name` |
| PUT | `/api/schemas/{table}` | 覆盖更新表结构 |
| DELETE | `/api/schemas/{table}` | 删除表结构；保留数据文件 |

删除有数据的表结构会返回 `409`。需要连数据一起删除时使用：

```text
DELETE /api/schemas/users
```

## 表数据接口

| 方法 | 路径 | 作用 |
| --- | --- | --- |
| GET | `/api/data/{table}` | 查询行，支持 `where`、`order_by`、`columns`、`limit`、`offset` |
| POST | `/api/data/{table}` | 新增一行；也接受 `{ "rows": [...] }` 批量新增 |
| PUT | `/api/data/{table}` | 按 `where` 批量更新，格式 `{ "values": {...}, "where": {...}, "limit": 1 }` |
| DELETE | `/api/data/{table}` | 按 `where` 删除，格式 `{ "where": {...}, "limit": 1 }` |
| GET | `/api/data/{table}/{index}` | 按列表下标读取一行 |
| PUT | `/api/data/{table}/{index}` | 按列表下标替换一行 |
| DELETE | `/api/data/{table}/{index}` | 按列表下标删除一行 |

`where` 可以是
JSON 对象，例如 `{"status":"启用"}`，也可以是核心层支持的条件表达式，例如
`age >= 18`。

如果表结构将多个列标记为 `primary_key: true`，这些列组成联合主键，列的
定义顺序就是主键值顺序。WEB 查询、更新和删除可以在 `where` 中同时给出
这些列，例如：

```text
GET /api/data/links?where={"site":"a","device":2}
```

对应的更新请求为：

```json
{
  "where": {"site": "a", "device": 2},
  "values": {"state": "down"}
}
```

## SQLite：加载 dbdef / E 文件

左侧 **加载 dbdef / E** 可在选中的数据库尚无数据表时使用。dbdef点击“预览”；E文件按“上传并解析 → 校验字段选择 → 确认”操作。每张表以表名为标签展示类型、主键、记录数及警告；切换标签保留各表设置，确认后逐表导入。支持UTF-8（含BOM）、GBK/GB18030和带BOM的UTF-16，默认自动识别。表或字段的中文名为空、`null`（不区分大小写）或 `\N` 时自动使用英文名；导入和保存结构时同步写入JSON、Python定义。业务数据中的 `null` 文本不受影响。

- `.dbdef`：读取旧版 `<table_info>` 和各表字段模块，生成/更新当前SQLite库中的表。当前库的全部结构导出至 `--schema-dir/<数据库名>.json` 和同名 `.py`，Python提供 `DATABASE_DEFINITION`、`TABLE_DEFINITIONS` 及各表dataclass，字段metadata与JSON一致。Python保留字字段使用别名，原名保留在metadata的 `eng_name` 中。
- `.e`：读取 `<表名>`、`@ 英文字段名`、可选 `// 中文字段名` 和 `# 数据`。新表按全部数据推断类型，页面默认第一字段为KEY，可逐列勾选联合主键。已有表沿用类型/主键，保留未提供的字段、增加文件中的新字段；定义导出同样以当前数据库为单位。
- E 数据支持“按主键合并”“追加”“替换整表数据”。合并时，文件未提供的原有字段值保留。重复主键仅警告，不阻断预览和加载：追加时保留已有/首次记录，跳过与数据库或文件内的冲突记录；合并和替换时采用文件内同一主键的最后一条记录。预览仍可人工调整 KEY（如联合主键）再确认导入。预览、执行结果和持久化日志记录重复/跳过数量及警告。空主键、无效类型转换、超范围整数仍会拒绝导入。字符串含空格/换行须用单引号或双引号包围；同种引号用连续两个转义；空字符串写 `''`，空值写 `\N`。
- dbdef 可以迁移有数据的表：保留同名字段，新增非主键字段为 NULL；删除非主键字段会删除对应数据，预览会列明。有数据时禁止变更已有字段类型、主键和保留字段的相对顺序，与表结构编辑页一致。
- 旧版 `max_record_num` / `data_length` 是导入源的描述信息，不写入新的 JSON。当前 JSON 严格结构契约保持不变。
- 数据库文件管理中的“处理报警”提供三种方式：**重新生成内置定义表**仅根据旧物理表生成 `c0、c1……` 兼容定义，不恢复丢失的业务字段名称；**删除未定义数据表**会先归档再移除文件；**重建数据表**要求已有 SQLite 内置定义，并按定义重建 `data` 表、索引和元数据。缺少业务定义时，应继续通过正式 dbdef/E/JSON 更新字段名称和显示属性。
- 预览不写业务表或定义文件。应用时重新检查数据/结构版本；先验证文件内全部表，再逐表提交。单张表的结构和数据在同一 SQLite 事务中更新，JSON/Python 使用原子替换和恢复日志同步。**多张表并非一个整体事务**：中途失败会列出已完成、失败及未处理表。结构变更后，外部长期持有数据库对象的程序应重新打开连接。
- dbdef 页面和旧 Base64 导入路径单文件最大20 MiB；E页面已改为下述128 MiB分块流程。最多200张表、单表最多100,000行。一般表结构表单的保存也同步更新Python定义。

E 文件例子：

```text
<meters>
@ id name value
// 编号 名称 功率
# 1 '北侧 电表' 12.5
# 2 南侧电表 18.5
</meters>
```

接口：`POST /api/manage/definition-preview` 和 `POST /api/manage/definition-import`。请求包含 `format: "dbdef" | "e"`、`text`（Unicode 文本）或 `content`（原始文件 Base64）、`encoding`、`mode`、`keys`。E 文件预览按全部数据行推断类型，新表默认首字段为 KEY；人工确认后的应用请求可附带 `definition_overrides: {表名: {columns: [{eng_name, data_type, primary_key}, ...]}}` 覆盖类型和主键。应用导入时必须回传预览结果的 `token`。自动化调用应检查响应的 `ok` 和逐表 `results`。

结果表格使用行标题、列标题和鼠标拖拽框选；Ctrl 支持增选，Shift 支持连续选择；单元格双击编辑，复制行和删除选中记录位于右键菜单。顶部只保留“结果展示 / 表结构”；表格底部通过“曲线 / 日志”标签切换，导出和清除按钮随标签切换。曲线与单元格双向同步，改动以黄色高亮；“保存改单元格”后写入数据库。底部共用分割条，向下拖到底部收起，向上拖动恢复；切换标签保留表格显示、选区和未保存修改。

左侧“显示字段”列表同时显示字段中文名、英文名和归一化数据类型（`INT`、`FLOAT`、`STR`、`BOOL`、`JSON`、`LIST`、`DICT`）；KEY 字段继续显示 `PK` 标识。

表结构的“显示类型”单击下拉框选择“文本 / 整数 / 浮点数 / 下拉框”，已有其他显示类型也会保留为可选项。旧定义中的“数字”在编辑页按 INT/FLOAT 分别显示为“整数 / 浮点数”，点击“保存结构”后写入数据库内置定义并同步导出 JSON、Python；仅浏览不改写文件。修改数据类型时，仍使用默认显示类型的字段会同步切换默认值，已选择的其他显示类型保持不变；改动继续以黄色高亮。

表结构新增“默认值”列，双击编辑并点击“保存结构”后写入 JSON 的 `columns[].default_value` 与生成的 Python 数据类。留空表示未设置；整数、浮点数按类型输入，布尔值填 `true` / `false`，文本可直接输入（空字符串填 `""`），对象/数组填 JSON。默认值会做类型、有限数值和 64 位整数范围检查，不能使用 SQL 表达式。新增记录自动填入默认值；SQLite 行接口及 Python `SQLiteDatabase` 新增时省略的字段也会使用默认值，显式 NULL 或自填值保持原样。默认值不回填已有记录，包括结构新增字段；修改、清除默认值只影响后续新增。复制行保留复制内容。未设置默认值的旧定义保持原有结构指纹，无需迁移；SQL 编辑框继续按语句中指定的值写入。

结果区的新增记录、导入、导出查询（JSON / CSV）、批量修改选中、删除选中记录及清空表统一放入右键菜单。表格单元格、标题和空白区域都可打开菜单，空表也能新增或导入。批量修改须先点击行标题选择行；“删除选中记录”支持行标题、单元格区域、列选区，按选区涉及的整行去重删除，确认窗口显示实际行数。待保存修改或新行不会禁用删除：新行仅从页面移除，已有记录按原始主键和版本删除；只移除被删行的待保存修改，保留其他行及新行。混合选择时先提交已有记录的原子删除，失败则页面全部内容保持原样；成功后再移除相应新行并同步计数、选区和曲线。已移除重复的“编辑记录 / 复制记录 / 选择或取消选择 / 删除记录”菜单。SQL 聚合或投影结果仍不可删除原记录。删除和清空须确认；导入、清空及批量修改仍须先处理待保存内容。菜单支持 Shift+F10 打开、方向键选择及 Escape 关闭。

右键“新增记录”先弹出条数输入框，默认 1 条，一次最多暂存 500 条新增记录；确认后在打开右键菜单的鼠标所在行之后连续插入新行（按字段默认值初始化），行号标为“新…”，不受其他选中行影响。在空白区域打开菜单时插入到当前页末尾，空表则从第一行开始。右键“复制选中行”将选中行（或选区涉及的整行）的完整内容存入页面行剪贴板，包括隐藏字段和尚未保存的值；再选目标位置，右键“插入复制行”在最后一个选中行之后插入副本。支持多行、重复插入及字段结构相同的表之间复制；复制后修改源行不会改变已复制的内容。新增和插入复制行均先暂存在页面，双击可填写 KEY 和其他字段，也支持批量粘贴；单元格改动黄色高亮。已有记录的 KEY 保持只读，新行保存后 KEY 也恢复只读。聚合或投影查询中新增空白行时，会先切换到当前表的完整可编辑查询。

点击“保存修改”直接调用 `PUT /api/manage/tables/{name}/cell-edits`，在同一事务内完成全部校验和写入。主键任一字段为空（含纯空白）、新增行之间主键重复或与数据库已有记录冲突时，弹窗列出问题并保留填写内容，整批不写入；复合主键按完整组合判断重复，检查覆盖数据库所有记录。独立 `validate-edits` 接口仍可用于只读预检查，但页面保存不再重复调用。两个接口均接受 `changes: [{ref, values}]` 与可选 `inserts: [完整记录]`，分别最多 500 条。未设置默认值且未填写的字段保存为 NULL。保存返回 `updated`、`inserted` 数量，并记录服务端持久日志。撤销修改会同时移除待新增行；刷新、切表、SQL 和自动刷新不会静默丢弃这些待保存内容。

结果中的普通单元格单击仅选中，双击才进入编辑；BOOL 复选框单击修改。Enter/Tab 确认并移动，点击其他位置结束编辑，Escape 取消本次编辑并恢复进入编辑前的值。修改通过保存操作或 Ctrl+S 写入数据库。已有记录的 KEY 列只允许选择；已有对象/数组字段双击打开记录编辑窗口，新行可在单元格内填写 JSON。未保存修改导致翻页失败时，页码、每页条数和当前数据保持一致。

SQL 无需以分号结尾，例如 `SELECT * FROM "devices"`。默认语句、执行后的输入和历史回填均省略末尾分号；输入带分号的旧语句仍可执行，字符串内容中的分号保持不变。

SQL 工具栏使用单行输入框，已移除独立的模板、历史和重置控件。点击输入框内的下拉箭头查看当前表的历史语句，输入内容可筛选历史；鼠标选择，或用上下键选中后按 Enter，仅填入待执行语句。点击“执行”、直接按 Enter 或 Ctrl+Enter 才提交当前 SQL，写入语句仍需确认。成功执行的最近 30 条语句去重置顶，按数据库路径和表名保存在当前浏览器，刷新后保留；取消或执行失败的语句不加入历史。查询区域保持单行高度，分割条仍可拖拽折叠和恢复。

选中单元格区域、多行或多列后，右键选择“左轴曲线 / 右轴曲线”可批量加入曲线。按选区中不同的行、列数量判断：行数大于列数时，每列一条曲线，以列名命名；行数小于或等于列数时，每行一条曲线，以 KEY（复合主键用 ` / ` 连接）命名。行列数量包含选中的非数值列，但只绘制数值字段；不连续选区中的空缺保持断点。

每条曲线分别按字段定义的数据类型筛选：同时选中 INT 和 FLOAT 数据时，忽略该曲线中的 INT 点及其横轴标签；仅有 INT 时正常绘制。FLOAT 字段即使当前值为整数（如 `1.0`）也保留；不同曲线之间不互相影响。此规则适用于右键追加的左右轴曲线，过滤后的点继续绑定原单元格，支持双向编辑。

曲线只在右键执行“左轴曲线 / 右轴曲线”后生成或追加，表格选区变化不会改变已有曲线。首次打开、切换表、SQL、页码或排序时清空曲线及图例，保持空白，不自动生成默认曲线。重复加入同一组选点只调整左右轴；图例的 × 可移除单条，“清空曲线”清除全部曲线。混合方向或不同横轴标签的曲线使用点序号对齐，游标显示各点实际标签。单元格编辑、曲线拖拽和保存后刷新保持绑定。只读 SQL 结果也可绘图，但不可拖拽修改；无 KEY 的结果按行序号命名。

图例位于曲线板右上角。点击曲线名称或色点可隐藏，再次点击恢复；隐藏项置灰并加删除线。隐藏曲线不参与坐标轴自动缩放、游标读数或拖拽编辑，原数据和曲线配置保留。图例中的左右轴下拉框和 × 按钮分别用于换轴、移除，与显示/隐藏相互独立。

曲线板右键菜单提供独立的“标题设置”：设置标题文字、颜色、字体、字号（8～72 px）、常规/粗体/斜体/粗斜体、顶部或底部的左/中/右位置及间距。标题留空则不显示；图表为标题自动预留高度，横轴标签与轴标题随布局移动。设置在当前浏览器保留，并用于 PNG 导出；未添加曲线时也可预先配置。“曲线设置”仅设置每条曲线的名称、坐标轴、线条及点样式。

结果中的 KEY（主键）列以浅绿色和 KEY 标记区分，始终排在数据列最左侧并冻结；已有记录不可编辑，新行保存前可填写。联合主键各列依次冻结，横向滚动、窗口缩放不会使其位置错乱。显示字段的取消/反选保留主键列。此规则不向 SQL 投影结果补入原本未查询的字段。

表格选中单元格区域、行或列后，按 Ctrl+C，或在右键菜单选择“复制选中区域”，会按制表符和换行符复制为可直接粘贴到 Excel 的二维文本。将焦点放在选区后按 Ctrl+V，或选择“粘贴到选区”，数据从选区左上角按行列展开；只读 KEY 单元格会跳过，其他字段先作为待保存修改并高亮，确认无误后点击“保存改单元格”。粘贴范围必须落在当前页和当前显示字段内，字段值仍按表结构类型校验。

SQL UPDATE 同样不允许改变现有记录的 KEY；新增或复制记录仍可填写新 KEY。曲线默认按行绘制并使用 KEY 命名；也可手动切换为按列绘制，此时使用字段名。单元格编辑保留 64 位整数精度；超过 JavaScript 精确整数范围的点仅供查看，不允许通过曲线拖动改值。左右轴设置可手动指定上下限，留空时自动缩放；横轴设置可修改轴标题和标签间隔。

与旧版 rtdbmng 的差异：目前没有 E 格式导出、独立菜单文件导入导出、导入时自动重编关键字、批量清空所选表、跨应用/主机/上下文切换以及商用库双向同步。备份恢复使用本项目 JSON 格式，不能直接读取旧版原生备份。这些旧版功能尚未移植，不能按“全量功能等价”验收。

## 整库快照与恢复

入口：**左侧表列表右键 → 整库快照 / 恢复**。选中数据库后可生成快照；未选择数据库时也可查看服务端已有快照并恢复。列表显示最新 50 份快照，旧快照文件仍保留，不自动删除。生成、恢复、下载的执行结果和失败原因进入操作日志；列表只读查询不反复写日志。

快照使用 SQLite 在线备份 API，每步 256 页，在固定的读取事务中取得一致快照，含 WAL 已提交内容、多表业务数据、索引、内置定义、默认值和库内回执。页面未保存草稿、服务日期日志和 `.web-tasks` 任务账本不包含在数据库文件中。采用共享数据库生命周期锁保护文件身份，外部 WAL 写入可继续；当前库后台任务仍按队列排序，其他数据库继续执行。超过 5 分钟的备份复制会中止；生成和恢复在发布前可取消。大快照持有读取事务期间可能暂缓 WAL 截断，应保留足够磁盘空间。

生成后执行 `quick_check`、定义指纹和记录数核对，再计算 SHA-256。完整快照与清单保存在数据目录 `.web-snapshots/`；只有发布完成的清单出现在列表。下载按 1 MiB 分块发送，无整库 Base64 或浏览器 Blob 副本，支持单段 HTTP `Range` 与 SHA-256 `ETag` / `If-Range`。不受 JSON 请求体和表级导出行数限制。客户端主动中止下载也会写失败日志。

恢复只允许**新数据库名**，已有数据库、定义文件及 WAL/SHM 均不覆盖。先复制到临时目录并核验 SHA-256、数据库完整性和元数据，更新数据库名称，最后短暂取得目录锁原子发布。数据库中保留原表名与所有列定义；同时导出新名称对应的多表 JSON/Python。导出失败会报告“数据库已恢复、定义导出待重试”，恢复日志支持下次读取或服务启动时补齐。数据库是定义的权威来源。任务结果落盘前进程中断时，可从已发布快照/恢复标记恢复结果，禁止自动重执行。即使原数据库文件已丢失，已有快照仍可在列表中下载、恢复；服务日期日志和任务账本如需迁移，须另外备份其目录。

接口：

- `GET /api/manage/database-snapshots`：服务快照列表；可加 `database=文件名` 只查一个库。
- `POST /api/manage/database-snapshots?database=文件名`：生成整库快照；可使用后台任务参数。
- `GET /api/manage/database-snapshots/{id}/download?database=来源文件名`：下载快照，支持单段范围；无效范围返回 416。
- `POST /api/manage/database-snapshots/{id}/restore?database=来源文件名`，内容 `{"name":"new_database","chn_name":"恢复副本"}`：恢复为新库；可使用后台任务参数。

验证：`python -m unittest web.test_database_snapshots`。性能对照：`python benchmarks/benchmark_database_snapshots.py`，在临时目录比较 JSON 表备份与整库快照生成落盘耗时；Python 分配峰值单独测量，不混入计时，不代表进程总内存。浏览器脚本 `benchmarks/verify_database_snapshots.js` 仅用于隔离的 workflow-lab，创建快照和新测试库，不可指向业务服务。

CSV/E 分块导入与E多表续作已实现，见后续章节。后续重点：快照保留策略、临时文件容量统计及服务日志/任务账本的配套备份。

参考：[SQLite Online Backup API](https://www.sqlite.org/backup.html)、[Python sqlite3.Connection.backup](https://docs.python.org/3/library/sqlite3.html#sqlite3.Connection.backup)。

## 本地整库快照分块上传与续传

入口：左侧表列表右键 → **整库快照 / 恢复** → **上传本地快照**。也可在“选择 / 新建数据库”的本地上传区域点击“上传整库快照（大文件 / 续传）”。上传校验完成后，填写新库名称并点击“恢复为新数据库”；同名数据库拒绝覆盖，原库和未保存编辑保留。

新入口接受带 RTDB 内置定义的自包含 `.sqlite3` 快照，单文件上限 **2 GiB**。在线数据库请先用“生成整库快照”取得一致副本，不要只复制仍在写入的主文件。原有 Base64 多文件上传接口仍保留 **16 MiB/文件**限制；新入口不接收 WAL/SHM 或 JSON 伴随文件。

浏览器每次读取 **1 MiB**，计算当前块 SHA-256 后发送二进制内容。服务端刷写数据和进度清单后才确认接收；相同位置、相同内容重复发送返回原进度，不追加两遍。断线可重试，暂停后保留已确认内容。刷新页面后需重新选择同一文件；续传前核对文件名称、大小、修改时间和每个已接收块的哈希，防止混入不同文件。浏览器 Web Crypto 需要 localhost 或 HTTPS 安全上下文。

临时会话保存在 `.web-uploads/`，最后更新后保留24小时，创建新会话时清理过期内容；最多8个未完成会话、声明文件总量最多8 GiB。暂停本身不延长保留时间。浏览器仅保存会话标识，不保存文件内容。放弃上传只清理临时内容，已经发布的快照与恢复出的数据库保留。

完整文件接收后，后台任务核对分块及整体 SHA-256（可输入预期值），执行 SQLite `quick_check`、内置定义指纹及记录数检查，通过后发布为服务端快照。校验可取消并保留已上传内容；进程中断后可重试校验，已经发布的后台任务结果可恢复。成功、失败、放弃、过期清理均记录日志，不逐块写成功日志。

接口（会话不绑定当前工作数据库）：

- `POST /api/manage/database-uploads`：`{id, filename, size, last_modified?, sha256?}`；id 为32位小写十六进制；同一标识、同一文件身份重复提交幂等。
- `GET /api/manage/database-uploads/{id}`：已确认偏移、状态及各块哈希。
- `PUT /api/manage/database-uploads/{id}/chunk?offset=字节偏移`：`Content-Type: application/octet-stream`、`X-Chunk-SHA256`；每块1 MiB，最后一块可不足1 MiB；按序发送。
- `POST /api/manage/database-uploads/{id}/finalize`：校验并发布快照，可使用后台任务参数。
- `DELETE /api/manage/database-uploads/{id}`：放弃临时上传。

验证：`python -m unittest web.test_database_uploads`；浏览器 `benchmarks/verify_database_uploads.js` 仅允许隔离 workflow-lab。性能脚本 `python benchmarks/benchmark_database_uploads.py` 在临时目录以 HTTP 比较 Base64 上传与分块上传、校验、恢复新库的全流程；两条路径检查相同记录与大字段内容。分块路径额外保留已校验快照，每块持久化确认，语义更强，不保证小文件更快。Python 内存分配峰值单独测量，不代表浏览器或进程总内存。

参考：[Blob.slice](https://developer.mozilla.org/en-US/docs/Web/API/Blob/slice)、[SubtleCrypto.digest](https://developer.mozilla.org/en-US/docs/Web/API/SubtleCrypto/digest)。后者不提供流式摘要，因此浏览器只对单块求哈希，完整文件摘要由服务端流式计算。

## CSV 流式导入

入口：选择数据库和目标表后，**左侧表列表右键 → CSV 流式导入**。采用既有表结构，显示字段类型与 KEY；先完成上传、全文件校验和样本预览，再选择追加、按主键合并或替换整表并确认。合并与替换需输入表名确认；有未保存的数据或结构修改时禁止导入。

新路径支持128 MiB、最多100,000条记录；须包含全部英文字段名，支持 UTF-8（含BOM）和 GB18030/GBK。支持引号、逗号及引号内换行；空值使用 `\N`，文本中的反斜杠按原CSV导出规则转义。文件内重复或空 KEY、列数不符、类型错误、无效编码及非有限数值均拒绝，错误显示行号或字段。单字段最多128 Ki字符。旧JSON/CSV文本接口保持原行为，E/dbdef结构导入暂未改为此路径。

浏览器复用1 MiB上传协议，每块持久化确认、哈希核对、暂停、刷新续传；上传会话固定绑定数据库、表和编码，切换目标不允许误导入。临时内容保留24小时，预览只回传前20条样本，长文本截取前160字符展示，完整值保存在服务端。暂存数据和已接收原文件都位于 `.web-uploads/{id}/`，创建新会话时清理过期内容，点击“清除临时上传”不删除已导入的业务数据。

服务端用 `csv.DictReader` 逐行解码和校验，转换后写入临时SQLite，利用唯一索引检查跨批次重复 KEY。确认导入后每批最多512条，使用一次数据库事务；追加冲突或任意批次写入失败均整体回滚。合并使用SQLite UPSERT，保留原记录 `_pos`，不逐行查询是否存在。替换在持有写锁后再次核对预览版本；目标结构变更或暂存文件损坏也会拒绝提交。

上传校验期间支持取消；真正提交开始后不可取消，避免“已写入但用户以为取消”的歧义。数据、导入去重标记和写入回执在同一事务提交；同一上传重复确认只返回原结果，改用另一方式重复执行则拒绝。日志记录校验、提交、失败与清理结果，后台任务结果支持重启恢复。

接口复用：

- `POST /api/manage/database-uploads`：除原有 `id/filename/size/last_modified` 外，增加 `kind:"csv"`、`database:"目标.sqlite3"`、`table:"表名"`、`encoding:"utf-8-sig"` 或 `"gb18030"`。
- 分块 `PUT`、进度 `GET`、取消暂存 `DELETE` 与整库快照相同；`POST .../{id}/finalize` 返回总记录数、样本、字段类型、预览token，不写目标表。
- `POST /api/manage/tables/{表名}/import?database={数据库}`：`{"upload_id":"上传编号","token":"预览token","mode":"append|merge|replace","confirm":"表名"}`；可使用后台任务参数。

验证：`python -m unittest web.test_csv_imports`。浏览器脚本 `benchmarks/verify_csv_imports.js` 只允许隔离 workflow-lab，测试超过16 MiB的文件、暂停续传、丢确认重试、预览和原子导入。基准 `python benchmarks/benchmark_csv_imports.py` 在临时目录比较旧文本CSV合并与新分块上传/预览/合并流程，包含磁盘和校验开销，不含HTTP与浏览器时间；内存测量为Python分配峰值，不是进程总内存。

参考：[Python CSV 文件读取](https://docs.python.org/3/library/csv.html)、[SQLite UPSERT](https://www.sqlite.org/lang_upsert.html)。

## E 文件分块导入与逐表续作

入口保持为“选择/新建数据库 → 左侧表列表右键 → 加载 dbdef / E”。选择 `.e` 后按以下流程操作：

1. **上传并解析**：每块1 MiB，支持暂停、刷新后重选原文件续传；完整文件逐行写入临时 SQLite，不将全部行装入浏览器或 Python 列表。每张表一页，类型推断使用全部数据行；新表默认第一字段为 KEY，已有表保留当前定义。
2. **校验字段选择**：人工选择各表类型和 KEY，服务器在磁盘暂存中校验所有数据，计算新增、更新、跳过和重复数量。类型或导入方式改变后必须重新校验；无效选择会撤销旧的可执行预览。
3. **确认并生成/更新**：输入目标数据库文件名确认。每张表的结构、数据及完成回执在同一个事务中提交，同时更新 SQLite 内置定义并导出该库的 JSON/Python 多表定义文件。追加模式保留已有/首次出现的主键，合并与替换采用最后一条，均显示重复警告。
4. **继续 / 核对导入**：单表失败整体回滚，先前已完成表保留。重试以数据库内的完成回执为准，跳过已提交的表，即使这些表后来又被编辑。数据库版本冲突需重新上传、重新校验；不会自动覆盖外部修改。计划开始提交后不可再修改其字段/模式，清除暂存不删除已提交数据。

限制：128 MiB；每表10万条、最多200张表、每表512字段；单字段128 Ki字符，单条含换行的逻辑行1 Mi字符。支持 UTF-8/BOM、GB18030/GBK、UTF-16/BOM 与自动识别。样本每表最多5条、前20列、每值80字符；只是显示截取，完整值照常校验和导入。128 MiB 是配置上限，并非每轮基准的实测文件大小。

多表导入是**逐表事务**，并非整文件的全有或全无。执行期间可在表与表之间取消；单表提交阶段不可取消。临时上传仍按24小时保留、创建新会话时清理；会话过期后不能用原编号继续。JSON/Python 导出失败会明确显示“已提交、导出待恢复”，下次访问通过恢复日志重新生成文件。dbdef 和旧文本 E API 保留原路径，未改成流式接口。

接口：创建上传使用 `kind:"e"`、明确的 `database`、`encoding`，其余分块端点与 CSV 相同。`finalize` 只解析及推断；`POST definition-preview?database=...` 接收 `upload_id/mode/definition_overrides` 并返回校验 token；`POST definition-import?database=...` 仅提交 `upload_id/token`。后台任务仍可查询、取消和恢复结果；中断后重试应创建新的任务请求编号，沿用相同 upload_id/token，由逐表回执去重。

验证：`python -m unittest web.test_efile_stream`；浏览器脚本 `benchmarks/verify_efile_stream.js` 使用隔离测试库。基准 `python benchmarks/benchmark_efile_stream.py` 比较原文本 E 完整流程和新分块解析/校验/导入流程，单独测量 Python 分配峰值；不含 HTTP、人工确认、数据库准备和结果核查。下一步重点是临时文件/快照空间统计、保留策略和容量限制。

## 服务存储维护

在左侧表列表右键选择“存储维护”，无需选择数据库。统一显示上传暂存、整库快照、后台任务账本、日期日志的文件数和逻辑字节数。统计读取文件元信息及任务账本，不读取快照或上传的大文件内容。

保留策略存于数据目录的 `.web-storage.json`：上传暂存默认 1 天、快照默认 30 天、任务详情默认 7 天，0 表示不清理。上传可续传有效期仍为 24 小时；创建上传时沿用过期清理但遵守保留策略与活动任务保护。任务结果正文沿用每小时/启动时的过期处理。快照、任务详情的完整清理需要人工预览和确认，不会因保存配置而立即删除文件。日期日志仍使用原日志页的保留与清理设置。

- `GET /api/manage/storage-maintenance`：统计、候选清单及有效期 10 分钟的清理令牌，每批最多 200 项。
- `PUT /api/manage/storage-maintenance`：保存 `upload_retention_days`、`snapshot_retention_days`、`task_retention_days` 三项整数天数。
- `POST /api/manage/storage-maintenance`：提交 `{ "token": "预览返回的令牌" }`，支持后台任务。执行前重新核对配置、文件身份/大小/更新时间及任务状态，变化时拒绝整批操作。令牌只能使用一次，服务重启后需重新预览。

活动上传、下载、快照生成/恢复通过共享锁保护，清理使用独占锁；有排队或运行中任务时保守地保留所有上传和快照。业务 SQLite 数据库、恢复回执、孤立快照、无法确认归属的临时目录、链接和未知文件均保留。任务只清理终态详情，永久保留请求编号、指纹和执行状态，防止旧请求重放；中断待核查任务不清理。账本空闲页供后续写入复用，不执行阻塞性的 VACUUM，也不承诺文件立即缩小。配置和清理结果/失败信息写入现有审计日志。

验证：`python -m unittest web.test_storage_maintenance`；性能测量：`python benchmarks/benchmark_storage_maintenance.py`（隔离临时目录，结果在 `output/storage-maintenance-benchmark.json`）。

## 健康检查

```text
GET /api/health
```

成功响应：

```json
{"ok": true, "service": "rtdb"}
```

