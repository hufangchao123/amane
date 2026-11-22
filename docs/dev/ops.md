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
- 后继任务只经 `TaskResult.followups` 进入完成事务, 不直接写队列; 任务判失败或崩溃时仍要排的后继走 `TaskHandler.failure_followups`, 由 worker 单独入队 (它们不属于失败任务的子任务)。
- `wash_apply` 与 `delete` / `organize` 争用同一库路径, 必须共享 `LibraryTaskLocks`。

## 体检与备份的判据

体检只覆盖「当前配置下一定出问题」的项, 未启用的功能一律通过; 三项约束是不写库、不修改文件、不发网络请求, 因此可以在请求内运行 (`SelfCheck` 的全部探测都在线程池内)。判据随功能演进只能新增「静默失败」类型: 如果某项在出错时用户仍能完成任务, 就不该让体检失败。

备份产物按时间戳命名, 份数按时间戳分组计算 (`keep_db` 管一次数据库备份的两个文件, `keep_assets` 管资源归档), 修剪只删除本类前缀。接口只覆盖生成与列举; 恢复走 `python -m amane.backup.restore` (`backup/restore.py`), 必须先停服务: 脚本自己取目标库的排他锁, 取不到即报「仍被占用」并退出, 覆盖前清掉旧 `-wal`/`-shm` (只换主库而留着旧 WAL 会被回放成损坏), 现库先留底 `*.pre-restore-<时间戳>` 再替换。`--dry-run` 只打印计划, 目标库已存在时必须给 `--yes`。备份不含 `config.toml` (配置里有凭据)。

`backup.dir` 空时用 `data_dir/backups`; 指向 `data_dir` 之内直接放行, 之外要求目录能自证归属: 不存在、空、或只装本工具产物, 否则须放空标记文件 `.amane-backup-root` (`backup/manager.py::ROOT_MARKER`)。`data_dir` 本身及其上级目录一律拒绝。校验失败抛 `BackupRootError` (RuntimeError 子类), 由 handler 转成任务失败。

## 洗版的分组与回收站

分组键含库 id、番号、CD 与有无码, 本地文件与 STRM 各成一组, 跨库不合并。本地择一依次比较清晰度档位、码率 (`size/duration`, 时长缺失时跳过该项而不拿体积冒充)、体积、容器、路径; STRM 的 `size` 恒为 0, 只比同目录、容器与路径, 绝不进体积比较。

回收站落在库根的**父目录**: `<库根父目录>/.amane_wash/<库根名>/<local|strm>/<库内相对路径>`, 与库同盘因此多为改名; 目标已存在时计入 `skipped`, 不覆盖。扫描、清点与空目录判定把 `.amane_wash` 与 `.amane_trash` 一并忽略 (`library/rules.py::is_in_trash`)。还原靠 `group_key` 里的库 id 与上述布局反推原位: 库被删除或重建后 id 变化, 还原会失败, 文件留在回收站可手工取回。

到期清理按记录逐条判定: 删掉或文件已缺失的记录推进 `purged`, 删失败 (权限、占用) 的文件仍在回收站, 记录保持 `trashed` 等下一轮重试 —— 推进 `purged` 会同时抹掉还原入口与重试机会。

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
- 中转单轮搬移上限由 `transfer.max_moves_per_run` 决定 (默认 5), 剩余文件下一轮继续: 上限只防单轮过久, 不表示放弃。

## 端点与提交

五个功能各有 `GET /api/{transfer|subtitles|wash|diagnostics|backup}/status` 只读端点, 任务入队统一走 `POST /api/tasks` 的判别联合。

五个提交模型 (`Transfer115Submission` / `SubtitleFetchSubmission` / `WashScanSubmission` / `WashApplySubmission` / `BackupSubmission`) 存在, 是为了满足 `tests/api/test_tasks.py` 的两条不变量: 每个 `TaskType` 都必须有提交模型 (`TaskSubmission`) 与结果模型 (`TaskResultPayload`)。前端把它们列为 `PanelOnlyTaskType`, 不出现在通用提交弹窗里 — 由各自页面按钮直接调 SDK。

`handlers/models.py` 里的 Payload / Result 与 `handlers/*.py` 里的 handler 分离, 结果模型带 `type: Literal[TaskType.X]` 判别字段。新增任务类型要同步五处, 少一处就有编译期或测试期报错挡着: `TaskSubmission` 联合、`TaskResultPayload` 联合、`api/support/task_resolve.py::resolve_submission` 的 `match`、`web/src/lib/exhaustive-maps.ts` 的 `TASK_TYPES`、`web/src/lib/task/display.ts` 的 `TASK_ICONS`; 另有 i18n 的 `settings.json`(配置字段) 与 `tasks.json`(提交字段 + `filters.*`) 两处编译期穷举检查。

### 字幕巡检的判据

`SUBTITLE_AUDIT` 遍历库内已入库的文件, 逐目录比对视频与字幕, 输出两项互不校正的计数: `missing` 取自库里
`has_subtitle` 标记, `orphans` 取自磁盘 —— 字幕文件名不以任何视频的 stem 开头即算配不上, 因为落位规则是
`<视频 stem>.<语言>.srt`. 判据只看文件名, 不判「来源番号与目标番号是否一致」: 那要下载历史, 库内没有这份历史.

扫描的目录只取已入库文件所在的目录, 扩展名取 `libraries.subtitle_extensions` (空列表跳过). 巡检只出清单,
不移动文件, 因此不取库锁; 清理配不上的字幕在回收站运维里做.

## 定时入口

`scheduler/cron.py::_execute_task` 的 `match` 有五个媒体运维分支, 每一支只做「把例行转成任务」:

| 例行类型 | 入队任务 | 载荷 |
|---|---|---|
| `transfer_115` | `TaskType.TRANSFER_115` | `Transfer115Payload(full=…)` |
| `wash_scan` | `TaskType.WASH_SCAN` | `WashScanPayload(kind=…, library_id=…)` |
| `backup` | `TaskType.BACKUP` | `BackupPayload(kind=…)` |
| `wash_purge` | `TaskType.WASH_APPLY` | `WashApplyPayload(purge_ids=到期记录 id)` |
| `trash_purge` | `TaskType.TRASH_PURGE` | `TrashPurgePayload(library_id, keep_days, dry_run=False)` |

`wash_scan` 只入队只读扫描: 洗版是破坏性动作, 定时任务只产出候选, 由人决定何时执行 `wash_apply`。
`trash_purge` 的例行固定 `dry_run=False`: 例行的意图就是真删, `dry_run` 只在手动提交时默认开启。

`media_refresh` 的例行有独立分支 (`RoutineType.MEDIA_REFRESH` → `TaskType.MEDIA_REFRESH`), 只让媒体服务器重扫, 不触碰库文件, 因此不列在上表。

## 历史回收站的清理

`.amane_trash` 是历史遗留目录: 现在的删除直接删文件, 不再往里搬 (见 `data-model.md`)。目录里只有相对路径, 没有原位信息, 因此**不提供还原** —— 猜原位会把文件搬进别人的番号目录。要还原就手动移出, 移出后按新文件重新入库。

`TRASH_PURGE` 按修改时间清理: `keep_days` 之内的留着, 其余永久删除, 删空的子目录一并回收, 回收站根目录保留。`dry_run` 默认开启, 只统计并给出前 20 条样例; 关掉才真删, 因此这一步不可回滚。删除发生在库内, 与整理 / 删除 / 洗版共用 `LibraryTaskLocks`。

`GET /trash/status` 只报规模 (`GET /libraries/{id}/cleanup/trash` 之外的一个汇总视角), 遍历在回收目录内进行, 因此进线程池。洗版回收站 `.amane_wash` 不在这里清理: 它有自己的 `wash_records` 与到期清理。

`wash_purge` 的例行载荷为空 (`WashPurgeRoutine`), 到期范围由 `library/wash/apply.py::expired_record_ids` 判定 (只取 `state=TRASHED` 且 `expires_at` 已过), 因此定时不会重算洗版候选 —— 重算法会把用户留着不洗的分组一起洗掉。无到期记录时不入队。

`RoutineType` 与 `TaskType` 的取值都按 VARCHAR 存储, 新增枚举成员不需要迁移; 前端 `ROUTINE_TYPES` 的穷举与 `RoutineSubmission` 判别联合共同约束取值集合。

## 演员头像校验

头像地址来自刮削, 站点换路径后库里会留下大量失效链接. `AVATAR_CHECK` 只查每个演员的**首图**: 首图可用说明来源还活着, 后面的备用地址不单独校验.

判定分三种, 只有 `broken` 允许清链接 —— 清空是不可逆动作, 而站点抖动与链接失效在客户端看起来一样:

| 结论 | 判据 | 可否清链接 |
|---|---|---|
| `ok` | 拿到字节且文件头认得出是图片 | — |
| `broken` | 内容不是图片; 下载失败且状态码是 404 / 410 | 可以 |
| `unknown` | 地址指向内网、协议不支持、超时、连不上、域名解析失败、其余 4xx 与 5xx | 不可以 |

内网单独成一种, 是因为本地媒体服务器常驻内网, 那不是坏链接, 清掉反而丢图. 状态码要在下载失败后再问一次 HEAD: 下载接口只回「失败」, 不给原因.

判定只读: 下载内容写进 `data/avatar-check/<随机段>/` 后立即删除, 整轮结束连空目录一起收. 目录带随机段, 是因为 worker 并发跑多个任务, 共用固定路径会互相读到写了一半的文件. 单张上限 8 MiB.

`clear_broken` 打开时把坏地址从 `image_urls` 里摘掉 (其余地址保留), 走 `remove_actor_image_url` 而不是 `update_actor`: 后者会把该字段并入手动字段锁, 一次「修坏图」就让后续刮削不再维护头像; 前者只改这一行, 不碰锁与来源, 也不会用旧快照覆盖并发写入的其它地址. 单个演员的意外异常计 `failed` 而不报废整轮, `failed > 0` 时任务判失败.

## 写操作留痕

`AuditMiddleware` 在响应返回前把写方法 (POST / PUT / PATCH / DELETE) 的方法、路径、脱敏后的查询串、状态码、耗时与来源地址写进 `audit_records`; 写入失败只记日志, 不改写响应状态。留痕因此让每个写请求多一次插入, 换来的是没有排队丢失与没有后台任务的生命周期问题。留痕在鉴权中间件之外, 因此被拒的写请求也会被看到. 不过 401 / 403 只记日志、不落库: 那些请求没有发生写入, 逐条同步 commit 会被高频匿名请求放大. 端点抛未处理异常时响应是 500, 中间件在 `finally` 里照样留一行 —— 最该追溯的失败不能恰好缺席。

查询串按与日志同一套规则脱敏 (`utils/urls.py::redact_query`), 请求体与响应体不记录 —— 其中可能有密钥与个人数据, 体积也不可控。就绪检查、接入引导与长连接不记。

保留条数是常量 `AUDIT_KEEP`, 每累计写入这么多条就按时间倒序修剪一次, 因此表的上限是它的两倍; 修剪失败不清零计数, 下一条留痕会再试。`GET /api/audit` 只读分页, `DELETE /api/audit` 清空: 清空动作本身也会留下一条记录, 被清掉多少另记在日志里 (记录已删, 数量只能在日志里看)。留痕是事后核对手段, 不承担合规审计职责: 它不防篡改, 也不落远端。

## 通知

`NOTIFY` 把一段标题与正文推到 `notify` 配置里的渠道, 四种渠道只在请求形状上不同 (`notify/channels.py` 一张表); `kind=digest` 时正文留空则由任务现算摘要: 队列计数、洗版回收站可用量与占用、115 中转待搬条数、最近五条失败任务。摘要只查库, 不扫盘, 因此可以挂在例行上 (`NOTIFY_DIGEST`)。

地址由使用者在配置里填写, 常常指向内网 (自建 Bark / Server 酱代理), 因此这里**不放内网出站限制** —— 与抓站点的出站策略不同, 也不该相同。地址里可能带密钥, 所以 `notify.url` 进脱敏名单, 导出任务记录时会被替换。

每次推送写一行 `notify_records` (种类、渠道快照、标题、正文摘要、结果), 保留条数由 `notify.max_records` 决定, 超出部分在下一次推送时删除。推送失败只把任务标为失败并带上渠道返回码, 不回滚任何业务动作; 未启用或地址为空时不发请求, 直接失败并写明原因。

推送标题上限 200 字、正文上限 2000 字 (`handlers/models.py` 的 `NOTIFY_TITLE_LIMIT` / `NOTIFY_BODY_LIMIT`): 载荷整体进任务表的 JSON 列, 批量提交时若不设限会被写成几 MB。记录层另有 `BODY_LIMIT` 截断, 两者不同 —— 前者拒收, 后者只是留痕时缩短。渠道地址 (`notify.url`) 在配置阶段校验: 非空必须是 http(s) 且不超过 2000 字, 免得推送时才报错。
摘要 (`kind=digest`) 只报最近 7 天的队列与失败、按任务类型的条数分布 (原有刮削整理与移植来的运维任务同列, 按条数取前 6 类)、媒体库文件数与缺外挂字幕数、洗版回收站占用、115 待搬条数; 时间窗靠 `count_tasks(since=…)` 与 `count_tasks_by_type(since=…)`, 不扫盘也不读日志。


## NFO 修补

`NFO_PATCH` 改写已有的 NFO 文件, 分两类: `kind=thumbs` 给 `<actor>` 块补 `<thumb>`, `kind=directors` 删掉 `<director>` 行。两类都只碰磁盘文本, 不写数据库, 也不改库内索引; 手工搬动过目录的库同样适用。

缩略图取 `Actor.image_urls` 的第一条, 与媒体服务器上的演员图表同源; 演员在库内没有头像时计 `missing_image`, 不写半截标签。演员块已有 `<thumb>` 的跳过, 因此重复执行不产生变更。写入走同目录临时文件加 `Path.replace`, 中途失败不会留下半截 NFO。

`dry_run` 默认开启: 只扫盘并报出将改动的文件数与前十个文件名。真跑时按媒体库取 `LibraryTaskLocks`, 与整理 / 删除 / 洗版互斥。库目录不存在或 NFO 读写失败计 `failed`, 该文件跳过, 整轮继续。

`GET /nfo/status` 只报库内演员总数与其中带头像的数量, 以及可选媒体库; 待补条数必须先预演, 因为那要遍历磁盘, 不适合放在请求里。

计数与落盘一致: 只有写成功才累加 `changed` / `filled` / `removed`, 写失败只计 `failed`, 否则结果会报出没写进去的改动。临时文件带随机段并在失败时删除, 并发任务互不覆盖、也不留 `*.patch-tmp` 残骸。

## 刮削后转移

库配置 `scrape_transfer` 打开后, 每次刮削结束都排一个 `SCRAPE_TRANSFER`: 成功时作为 `followups` 随完成事务入队, 失败 (含 handler 崩溃) 时由 worker 经 `failure_followups` 入队。两处都 `priority=-1`, 不抢占交互提交的任务。

开启后库根 (`path`) 是成品目录: 待整理文件先落在 `scrape_input_dir`, 刮削成功搬进库根, 失败搬进 `scrape_fail_dir`。输入目录必填, 且与库根、失败目录**互不重叠** (相等或互相包含都拒绝, 422): 重叠时搬运结果会被扫描重新登记, 或刚搬出的文件被扫回, 形成来回搬运。失败目录可留空, 此时失败的文件留在输入目录, 便于直接重试。

扫描与监控都覆盖输入目录, 库根不再需要外部监控: 成品只由本服务搬进去。整库刷新会按库根跑一趟后再补一趟输入目录 (子目录范围的刷新不动输入目录), 监控则直接把观察点放在输入目录。

搬走的是视频与它同前缀的附属文件: 前缀取视频 stem 与解析出的番号两者, 因此整理前的 `ABC-123.nfo` 与整理后的 `ABC-123-1080p.srt` 都能跟上; 后缀须在库的字幕扩展名或附属集合 (`.nfo` / 图片) 内, 同前缀的另一份视频靠后缀排除。目的目录重名加序号, 不覆盖已有文件。

库内记录改指新路径, 文件仍在库里被跟踪: 可再次刮削、可再整理、可洗版。输入目录里搬空的那一层删掉, 库根保留。整批或单个文件失败时任务判失败并列出未搬动的文件名, 失败不影响已完成的刮削结果。



- 不新增鉴权豁免。五个路由都走全局 token 中间件。
- 不在 `api/routes/` 里跑文件遍历或网络请求。
- 不写第二个状态源 (旧面板的 JSON / 内存字典) 与数据库并存。
- 不用 `hasattr` / `getattr` 探测可选依赖, 缺失就走 typed 分支。
