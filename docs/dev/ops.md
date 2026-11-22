# 媒体运维功能 (中转 / 字幕 / 洗版 / 体检 / 备份)

> 五个阶段包: `src/amane/transfer`、`src/amane/subtitles`、`src/amane/library/wash`、`src/amane/diagnostics`、`src/amane/backup`。
> 本文只写跨模块契约与仍生效的禁止事项; 端点形状、字段与默认值读源码或 `web/openapi.json`。
> 路由见 [api.md](api.md), 任务边界见 [task-system.md](task-system.md), 库文件与回收站见 [data-model.md](data-model.md)。

## 边界

| 包 | 职责 | 不变量 |
|----|------|--------|
| `transfer` | 网盘侧增量搬移与逐文件状态 | 只读网盘、只写 STRM 根目录; 不生成 STRM 内容 (`organize/strm_content.py` 负责) |
| `subtitles` | 从字幕来源取回字幕文件 | 放置一律经 `organize.subtitles`; 不直接改库内路径 |
| `library/wash` | 同组多份文件择一保留, 其余入回收站 | 移动必须持 `LibraryTaskLocks`; 落选只搬进 `.amane_wash`, 不 `os.remove`; 真删只发生在到期的清理 |
| `diagnostics` | 只读探针 | 不写库、不改文件; 超时长的深检走任务 |
| `backup` | 数据库在线备份与配置打包 | 备份库用 SQLite 在线备份接口, 不复制正在写入的文件 |

`library/wash` **不**并入 `amane.library` 的公共导出面: 配置层 (`config/manager.py`) 依赖 `library`, 汇入会形成循环导入。路由与 handler 直接用完整包路径导入。

## 任务类型

`transfer_115` / `subtitle_fetch` / `wash_scan` / `wash_apply` / `backup` 五类经 `AsyncWorker` 消费, 由 `app/runtime.py::build_handlers` 注册。
`wash_scan` 与 `wash_apply` 是**两个** handler (`WashScanHandler` / `WashApplyHandler`) 与两套 payload: 扫描只读出候选, 执行按候选移动; 前者不取 `LibraryTaskLocks`, 后者必须取。

`WashApplyPayload` 的三组入口可以同时给: `restore_ids` 与 `purge_ids` 来自回收站列表, `group_keys` 来自候选列表, 执行顺序固定为还原 → 清理 → 洗版; 三者全空表示按当前策略重算后执行全部候选, 只带还原或清理时不顺手洗全库。

- 长循环、批量搬移、批量下载、批量移动一律在这些 handler 里, 不进请求线程。
- 同步 I/O (网盘列表、字幕下载、文件移动、打包) 走 `utils/threads.py` 的 `@in_thread`, 遵守 5 ms 事件循环预算。
- 后继任务只经 `TaskResult.followups` 进入完成事务, 不直接写队列。
- `wash_apply` 与 `delete` / `organize` 争用同一库路径, 必须共享 `LibraryTaskLocks`。

## 体检与备份的判据

体检只覆盖「当前配置下一定出问题」的项, 未启用的功能一律通过; 三项约束是不写库、不修改文件、不发网络请求, 因此可以在请求内运行 (`SelfCheck` 的全部探测都在线程池内)。判据随功能演进只能新增「静默失败」类型: 如果某项在出错时用户仍能完成任务, 就不该让体检失败。

备份产物按时间戳命名, 份数按时间戳分组计算 (`keep_db` 管一次数据库备份的两个文件, `keep_assets` 管资源归档), 修剪只删除本类前缀。自动化只覆盖生成与列举, 恢复是手工操作: 停服后覆盖 `data_dir` 内的库文件与 `resources` 目录。备份不含 `config.toml` (配置里有凭据)。

`backup.dir` 空时用 `data_dir/backups`; 指向 `data_dir` 之内直接放行, 之外要求目录能自证归属: 不存在、空、或只装本工具产物, 否则须放空标记文件 `.amane-backup-root` (`backup/manager.py::ROOT_MARKER`)。`data_dir` 本身及其上级目录一律拒绝。校验失败抛 `BackupRootError` (RuntimeError 子类), 由 handler 转成任务失败。

## 洗版的分组与回收站

分组键含库 id、番号、CD 与有无码, 本地文件与 STRM 各成一组, 跨库不合并。本地择一依次比较清晰度档位、码率 (`size/duration`, 时长缺失时跳过该项而不拿体积冒充)、体积、容器、路径; STRM 的 `size` 恒为 0, 只比同目录、容器与路径, 绝不进体积比较。

回收站落在库根的**父目录**: `<库根父目录>/.amane_wash/<库根名>/<local|strm>/<库内相对路径>`, 与库同盘因此多为改名; 目标已存在时计入 `skipped`, 不覆盖。扫描、清点与空目录判定把 `.amane_wash` 与 `.amane_trash` 一并忽略 (`library/rules.py::is_in_trash`)。还原靠 `group_key` 里的库 id 与上述布局反推原位: 库被删除或重建后 id 变化, 还原会失败, 文件留在回收站可手工取回。

## 数据

`transfer_items` 以网盘文件 ID 为唯一键, 记录逐文件状态; `wash_records` 一行对应一次「留一个、移一个」, 指向回收站内的实际位置。二者只由所属任务写入, 路由只读。

面板时代的 `115-transfer-state.json` 等状态文件**不再读写**; 迁移期允许一次性导入, 之后以库为准。

## 字幕补全的目标

只挑 `status=scraped` 且没有外挂字幕的媒体文件; 落位要按番号找到元数据条目, 缺条目记为失败而不是缺口。同一目录已有一份字幕后, 后续文件按「已有外挂字幕」跳过, 因此一次任务通常只落一份。

外部站点地址 (115 webapi、字幕来源) 的连接都经 `net/egress.py` 判定: 默认拒绝内网地址, 这是防 SSRF 的硬约束, 本地联调要连伪造站点时只能在测试侧放行回环地址。

## 配置

`transfer` / `subtitles` / `wash` / `backup` 四个 section 挂在 `HotSettings`, 由 `/api/config` 的 schema 直接出表单。

- 密钥只放 section 内的字符串字段, 不进日志、不进任务 payload。
- `enabled` 与实际可用分离: 缺凭据或目录时服务层报未启用, 任务不会半途失败。
- 关闭某项功能不等于留下残留任务: 派生入口先看 `enabled`。

## 端点与提交

五个功能各有 `GET /api/{transfer|subtitles|wash|diagnostics|backup}/status` 只读端点, 任务入队统一走 `POST /api/tasks` 的判别联合。

五个提交模型 (`Transfer115Submission` / `SubtitleFetchSubmission` / `WashScanSubmission` / `WashApplySubmission` / `BackupSubmission`) 存在, 是为了满足 `tests/api/test_tasks.py` 的两条不变量: 每个 `TaskType` 都必须有提交模型 (`TaskSubmission`) 与结果模型 (`TaskResultPayload`)。前端把它们列为 `PanelOnlyTaskType`, 不出现在通用提交弹窗里 — 由各自页面按钮直接调 SDK。

`handlers/models.py` 里的 Payload / Result 与 `handlers/*.py` 里的 handler 分离, 结果模型带 `type: Literal[TaskType.X]` 判别字段。新增任务类型要同步五处, 少一处就有编译期或测试期报错挡着: `TaskSubmission` 联合、`TaskResultPayload` 联合、`api/support/task_resolve.py::resolve_submission` 的 `match`、`web/src/lib/exhaustive-maps.ts` 的 `TASK_TYPES`、`web/src/lib/task/display.ts` 的 `TASK_ICONS`; 另有 i18n 的 `settings.json`(配置字段) 与 `tasks.json`(提交字段 + `filters.*`) 两处编译期穷举检查。

## 定时入口

`scheduler/cron.py::_execute_task` 的 `match` 有三个媒体运维分支, 每一支只做「把例行转成任务」:

| 例行类型 | 入队任务 | 载荷 |
|---|---|---|
| `transfer_115` | `TaskType.TRANSFER_115` | `Transfer115Payload(full=…)` |
| `backup` | `TaskType.BACKUP` | `BackupPayload(kind=…)` |
| `wash_purge` | `TaskType.WASH_APPLY` | `WashApplyPayload(purge_ids=到期记录 id)` |

`wash_purge` 的例行载荷为空 (`WashPurgeRoutine`), 到期范围由 `library/wash/apply.py::expired_record_ids` 判定 (只取 `state=TRASHED` 且 `expires_at` 已过), 因此定时不会重算洗版候选 —— 重算法会把用户留着不洗的分组一起洗掉。无到期记录时不入队。

`RoutineType` 与 `TaskType` 的取值都按 VARCHAR 存储, 新增枚举成员不需要迁移; 前端 `ROUTINE_TYPES` 的穷举与 `RoutineSubmission` 判别联合共同约束取值集合。

## 禁止事项

- 不新增鉴权豁免。五个路由都走全局 token 中间件。
- 不在 `api/routes/` 里跑文件遍历或网络请求。
- 不写第二个状态源 (旧面板的 JSON / 内存字典) 与数据库并存。
- 不用 `hasattr` / `getattr` 探测可选依赖, 缺失就走 typed 分支。
