# ADR 0004：全量数据的可恢复清洗与原子发布

- 状态：已接受
- 日期：2026-08-25
- 决策范围：SPARCS 全量清洗、质量验收与 MySQL 发布

> 实施状态：本地 SPARCS 2021 全量文件已完成流式清洗、质量核对、
> staging 导入和 MySQL 原子切换，旧的 1,000 行表仍作为回滚副本保留。
> 后台任务、断点续跑、控制面 `background_jobs`/`dataset_versions` 接入和
> 运维审批后的备份回收仍未完成。

## 背景

SPARCS 2021 原始 CSV 约 794 MiB，包含 2,101,588 条数据记录。开发期脚本曾通过
`DROP TABLE` 或清空活表后重新装载 1,000 行样本；这种路径不适用于成品：清洗失败、
MySQL 警告、进程中断或网络断连都可能让线上表为空、半成品或无法判断实际版本。

医疗数据导入还必须证明以下事实，而不能只以脚本退出码作为成功依据：

- 实际读取的是经过治理的 33 列原始 schema；
- 清洗后是固定的 34 列 canonical schema；
- 输入、输出和拒绝行数可对账，拒绝阈值未超限；
- 原始文件与清洗文件在任务期间没有被替换；
- 非新生儿记录的 `BirthWeight` 已统一为 `NULL`；
- 新数据完成行数、质量和索引检查前，当前活表始终可用；
- 发布失败、确认丢失或并发任务不会误删另一个任务的数据；
- 每次尝试保留不可覆盖、无患者明细的机器可读审计。

## 决策

采用“两段产物 + staging + 原子重命名”的发布流程。

```text
原始 CSV（只读）
  -> 流式清洗到同目录临时文件
  -> profile.json + manifest.json
  -> manifest 最后发布，成为清洗提交点
  -> 导入任务校验 manifest、canonical header、大小和 SHA-256
  -> 任务私有 hard-link 快照绑定已验收字节
  -> MySQL 独立 staging 表（CREATE 同一语句写 task/source marker）
  -> 行数、业务规则、LOAD DATA warning 和索引验收
  -> 单条 RENAME TABLE 原子切换 staging/live/backup
  -> 独立连接按 marker 与旧表身份指纹对账实际发布状态
  -> 追加任务专属 JSONL import audit 快照
```

### 清洗提交点

清洗器使用流式 `csv.DictReader`/`DictWriter`，不得把 200 万行一次性装入内存。
清洗文件先写到目标目录内的临时文件并 `fsync`，成功后再原子替换目标文件。profile
和 manifest 不含患者行，只保存任务 ID、schema 版本、源文件大小/mtime/SHA-256、
输出 SHA-256、行数、拒绝数、质量状态和稳定错误码。

manifest 最后发布。导入器只接受 `status=succeeded`、schema 版本匹配、
`source_complete=true`、行数对账、拒绝阈值通过且输出路径/SHA-256 匹配的 manifest。
命令行给出的自报行数或自报哈希不能绕过 manifest，`--limit` 产生的开发样本不能发布为
活表。

### 数据规则与质量门

一期质量门至少包括：

- 原始列集合与行宽精确匹配；
- canonical 列名和顺序精确匹配；
- 输入行数 = 输出行数 + 拒绝行数；
- 默认拒绝阈值为 0，显式放宽必须进入审计；
- 金额转换为十进制，统一缺失表示；
- `AdmissionType != Newborn` 时 `BirthWeight IS NULL`；
- staging 行数与清洗 manifest 完全一致；
- `LOAD DATA` 产生任何 warning 时拒绝发布；
- 发布前后重新核对清洗文件 stat 和 SHA-256，防止校验与读取之间被替换。

当前数据没有可证明唯一患者身份的稳定主键，因此不能声称已经完成患者级去重。后续若
数据提供方给出业务键，必须先记录业务定义和保留规则，再新增去重质量门。

### MySQL 发布与恢复

导入只写任务专属 staging 表。发布前创建所需索引并完成质量查询。活表切换使用一条
MySQL `RENAME TABLE`：旧 live 改名为任务专属 backup，staging 同时改为 live。

MySQL DDL 会隐式提交，客户端异常并不等于服务器没有执行。因此：

- 发布任务使用 MySQL advisory lock 保证同一分析数据集同一时刻只有一个发布者；
- 发布前在持锁连接上用 `IS_USED_LOCK(...)=CONNECTION_ID()` 重新证明锁所有权；
- staging 的首条 `CREATE TABLE` 就携带不可变 task/source marker，避免建表确认丢失后
  出现无法判定归属的临时表；marker 随表重命名进入 live；
- `RENAME TABLE` 返回或抛错后，都使用新连接读取 live/staging/backup marker、行数、
  table comment 和 schema fingerprint；
- 只有 marker 证明当前 live 属于本任务时，任务才能声明成功或执行受控恢复；
- 无法唯一判断时标记为 `unknown` 并停止自动删除，交由运维按审计恢复；
- 不允许仅凭表名或相同行数盲目回滚；
- 旧表默认保留。清理 backup 是发布成功审计之后的独立运维动作，不与导入成功混在
  同一故障域。

任务专属 import audit 默认使用 task ID 命名、权限为 `0600`，不得覆盖 CSV、manifest、
profile、任务快照或历史审计。审计采用 append-only JSON Lines；每次追加一份完整状态
快照并 `fsync`。进程中断最多留下不完整的末行，之前的完整检查点仍可读取。文件通过
`O_NOFOLLOW` 的同一描述符校验和追加，路径被并发替换时不会写入替换文件。日志和审计
不保存患者数据、完整行、数据库密码或连接串。

### 控制面与后台任务

当前脚本是受控的单机运维入口。成品接口接入时，MySQL `background_jobs` 保存任务状态、
幂等键、阶段、heartbeat 和稳定错误码，`dataset_versions` 保存组织、数据集版本、来源
摘要、行数和 active/superseded 状态。队列消息只携带 `job_id`。

控制面记录不能替代表自身 marker：控制表事务与 `RENAME TABLE` 不是同一原子事务，
发生确认丢失时仍必须通过随表移动的 marker 判断 live 的实际身份。

## 本地全量验收事实

2026-08-25 在本地开发 MySQL 完成一次全量验收：

- 原始数据行：2,101,588；原始文件 SHA-256：
  `185808e20900c0499f7974d5ac9c05f0909df506bc088a244443bff895ca2219`；
- 清洗输出行：2,101,588；拒绝行：0；清洗文件 SHA-256：
  `d815d5d9b64680b3308636e408f4b291e2711918787a1cfd1d4a58a3635456e0`；
- MySQL live 行：2,101,588；非新生儿非空出生体重违规：0；
- 205 个非空设施 ID，另有 10,642 行缺少设施 ID；缺少设施 ID 的记录不会进入任何
  facility-scoped 产品查询，不能被静默分配给组织；
- 原 1,000 行开发表保留为
  `inpatient_backup_876dcce267b14044bfe85e5270b6207f`，尚未批准删除；
- 全表按年龄组平均费用聚合本机约 1.76 秒；单设施同类聚合约 0.10 秒。该结果仅为本机
  基线，不替代生产并发与容量压测。

原始、清洗、profile、manifest 和 audit 文件都位于 Git 忽略范围，不得提交仓库。

## 后果

正面影响：

- 清洗失败和 staging 失败不会影响当前 live；
- 发布是原子的，并可根据 marker 对账 ACK 丢失后的实际状态；
- 旧版本默认可回滚；
- 每次导入都有源文件和输出文件的可验证事实；
- 快速 `LOAD DATA` 与较慢的参数化批量 insert 共用同一质量门。

成本和限制：

- 发布期间需要同时容纳 live、staging 和 backup，必须预留磁盘空间；
- 对 800 MiB 级文件进行加载前后 SHA-256 会增加顺序读成本；
- 当前尚无断点续跑、拒绝文件、自动 dataset-version 激活或后台 worker；
- 缺失 facility ID 和没有业务主键的问题需要数据治理决定，不能靠技术默认值掩盖。

## 被否决的替代方案

- **先 DROP/DELETE live 再导入**：故障时直接造成服务不可用或半成品可见。
- **单个大事务插入 live**：DDL 和 `LOAD DATA` 行为、连接中断及 200 万行回滚成本不可控。
- **只校验行数**：相同行数的数据集仍可能来自错误文件或错误列顺序。
- **只用 Redis 锁**：租约失效不能证明表所有权；数据库 marker 和状态对账才是正确性边界。
- **导入成功后立即删除 backup**：审计或后续校验失败时失去可恢复版本。
- **把原始/清洗数据提交 Git**：违反医疗数据与仓库体积边界。
