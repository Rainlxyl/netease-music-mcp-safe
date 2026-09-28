# 决策记录

本文件追加记录长期技术或方法决策。不得删除历史条目；替代旧决策时，将旧条目标记为 `superseded` 并互相链接。

## 决策索引

| ID | 日期 | 状态 | 决策 | 替代者 |
|---|---|---|---|---|
| DEC-001 | 2026-08-03 | active | 默认只读，写入能力仅显式启用 | 无 |
| DEC-002 | 2026-08-03 | active | 播客能力保持只读并严格区分 ID 语义 | 无 |
| DEC-003 | 2026-08-03 | active | 混合工作区必须按任务边界选择性暂存 | 无 |
| DEC-004 | 2026-08-07 | active | 托管访问使用 OAuth 授权码流程与 PKCE S256 | 无 |
| DEC-005 | 2026-08-07 | active | 写入采用单次调用、服务端审计与显式幂等 | 无 |
| DEC-006 | 2026-08-07 | active | UTC 规范存储，IANA 时区仅派生展示语义 | 无 |
| DEC-007 | 2026-09-28 | active | 网易云登录态由持久 session 优先、环境变量回退统一管理 | 无 |
| DEC-008 | 2026-09-28 | superseded | web QR context 与短期网页登录共用单次内存 attempt | DEC-009 |
| DEC-009 | 2026-09-28 | active | 8821 由原网页登录页承接网易官方交互验证 | 无 |

## 决策记录

### DEC-001：默认只读，写入能力仅显式启用

- 日期：2026-08-03
- 状态：active
- 决策：`MCP_READ_ONLY=true` 是默认配置；只有显式设为 `false` 时才注册写入工具。写入能力必须继续保留输入校验、审计、幂等和明确的失败边界。
- 背景：项目连接个人网易云账户，错误写入可能造成难以恢复的数据变化。
- 理由：默认隐藏写入工具可缩小误操作面；校验、审计和幂等保护可降低重复或不完整写入风险。
- 证据：`README.md` 的“当前已实现”和“安全边界与 OAuth”；当前 `server.py`。
- 考虑过的替代方案：
  - 默认开放写入：拒绝，因为会扩大误操作面，并违背现有安全定位。
  - 仅依赖调用模型自行避免写入：拒绝，因为不能替代服务端能力边界。
- 影响：工具注册、环境变量、写入接口、测试和文档均须保持一致。
- 重新考虑条件：出现明确的新产品需求，并具备同等或更强的服务端安全保证。
- 替代：无
- 被替代：无

### DEC-002：播客能力保持只读并严格区分 ID 语义

- 日期：2026-08-03
- 状态：active
- 决策：播客工具不扩展到订阅写入、点赞、评论、播放控制、下载或收听进度；`radio_id`、`program_id`、`main_track_id` 与普通 `song_id` 不得混用。缺失的播放时间、序号或个人行为数据不得猜测。
- 背景：网易云播客接口未公开，不同入口字段不完整且语义可能不同。
- 理由：只读和保守规范化可防止把公共聚合值解释为个人状态，也避免错误写入或虚构数据。
- 证据：提交 `fac6c3c74aeaaf00b0b71a0e4d6d62e9b468eb1a`；`README.md` 的“播客读取与近期时间线”；`server.py` 与 `tests/test_server.py`。
- 考虑过的替代方案：
  - 把 `main_track_id` 当作普通歌曲 ID：拒绝，因为二者契约不同。
  - 扫描全部订阅或节目历史来补全详情：拒绝，因为详情接口可按节目 ID 查询，扫描会扩大范围并制造“完整历史”错觉。
  - 为缺失时间或序号生成推测值：拒绝，因为缺少可靠上游证据。
- 影响：播客 schema、规范化函数、批量结果、错误处理、测试和文档。
- 重新考虑条件：上游提供稳定且可验证的正式接口，并明确区分个人数据与公共统计。
- 替代：无
- 被替代：无

### DEC-003：混合工作区必须按任务边界选择性暂存

- 日期：2026-08-03
- 状态：active
- 决策：混合工作区必须先检查和分类；只暂存当前任务明确拥有的文件或差异，并在提交前复核暂存区统计、文件列表和完整差异。未跟踪文件默认保留，除非用户明确决定提交、归档或删除。
- 背景：项目曾同时存在产品 README 重写、功能增量和多份过程文档；提交 `fac6c3c` 只选择性纳入播客相关 README 增量，2026-08-07 收尾也按文件边界拆分三笔提交。
- 理由：避免覆盖、误提交或重新归属用户现有工作。
- 证据：提交 `fac6c3c` 的文件边界；2026-07-30 的历史发布记录；2026-08-07 项目收尾记录。
- 考虑过的替代方案：
  - 整体暂存 `README.md`：拒绝，因为会把既有重写混入无关提交。
  - 使用清理、重置或覆盖恢复干净工作区：拒绝，因为会损坏用户工作。
- 影响：所有后续提交、发布和文档任务的暂存策略。
- 重新考虑条件：无；即使用户授权整体提交，也应先检查内容和暂存边界。
- 替代：无
- 被替代：无

### DEC-004：托管访问使用 OAuth 授权码流程与 PKCE S256

- 日期：2026-08-07
- 状态：active
- 决策：托管客户端使用 OAuth 授权码流程、动态客户端注册、最小 scope 与强制 PKCE `S256`；静态 Bearer token 只用于受控客户端，不得暴露给托管聊天客户端。
- 背景：服务连接个人账户，托管客户端不能安全持有网易云 Cookie 或长期服务端秘密。
- 理由：短期 access token、可续期 refresh token、一次性授权码和 PKCE 能把客户端权限限制在明确的 `netease.read` / `netease.write` scope 内。
- 证据：`server.py` 的 OAuth 元数据、授权码与 token 实现；`tests/test_server.py` 的 OAuth/PKCE 测试；`README.md` 的“安全边界与 OAuth”。
- 影响：扩大写入 scope 必须重新授权；Cookie、`MCP_ACCESS_TOKEN` 与 `MCP_OAUTH_PASSWORD` 只留在各自服务端边界。
- 重新考虑条件：改用具备同等或更强安全属性的标准授权提供方。
- 替代：无
- 被替代：无

### DEC-005：写入采用单次调用、服务端审计与显式幂等

- 日期：2026-08-07
- 状态：active
- 决策：公开写入工具在一次 MCP 调用中完成，同时保留参数与所有权校验、写前/写后状态、脱敏审计、失败分类、可撤销性判断和可选 `idempotency_key`；`create_curated_playlist` 强制提供幂等键。
- 背景：提交 `2de793d1` 已移除旧的 preview approval flow。`preview_operation`、`preview_token` 和“先预览再执行”的用户流程不是当前设计；旧 preview 环境变量仅触发弃用提示，不改变行为。
- 理由：单次调用减少客户端状态耦合与过期预览失败，服务端校验、审计和显式幂等仍负责可靠性边界。
- 证据：提交 `2de793d1`；`server.py` 的写入执行核心；`tests/test_server.py` 的 single-call、审计与幂等回归测试；`README.md` 的“单次写入仍保留后端保护”。
- 影响：不得重新要求用户创建 matching preview；部分成功或结果未知的写入不得自动重试；JSON-RPC request ID 不替代持久幂等键。
- 重新考虑条件：出现经过验证、不会恢复旧客户端状态耦合的新交互设计。
- 替代：旧 preview approval flow（历史实现，已由 `2de793d1` 淘汰）
- 被替代：无

### DEC-006：UTC 规范存储，IANA 时区仅派生展示语义

- 日期：2026-08-07
- 状态：active
- 决策：时间戳以 UTC 作为规范存储和过滤语义；`MCP_DEFAULT_TIMEZONE` 必须是有效 IANA 时区，只用于派生本地展示字段与日期语言语境。
- 背景：部署区域、操作系统时区、夏令时和跨日期边界会导致模糊的“今天”或本地时间解释。
- 理由：UTC 保持稳定排序与过滤，IANA 时区可正确处理历史偏移和夏令时；网易云仍负责日推刷新边界。
- 证据：提交 `1e3da513`；`server.py` 的 timezone helper；`tests/test_server.py` 的时区回归测试；`README.md` 的“时间与时区”。
- 影响：不得使用固定偏移量替代 IANA 名称，也不得声称服务端时区能改变网易云日推内容或刷新时间。
- 重新考虑条件：引入经过设计的每用户或每调用时区，并继续保持 UTC 规范存储。
- 替代：无
- 被替代：无

### DEC-007：网易云登录态由持久 session 优先、环境变量回退统一管理

- 日期：2026-09-28
- 状态：active
- 决策：所有网易云请求统一从 session manager 取得 Cookie 与 CSRF；优先使用 SQLite 中有效的 runtime session，其次验证并可导入 `NETEASE_COOKIE`，均不可用时返回机器可识别的认证错误。二维码登录按“生成 key / 返回安全二维码 payload / 客户端扫码 / 显式查询状态 / 成功后持久化”执行，不启动后台高频轮询。登录验证结果缓存 15 分钟；模糊 403 必须结合一次登录态验证后才能归类为 `NETEASE_AUTH_EXPIRED`。
- 背景：旧实现直接读取进程级 `NETEASE_COOKIE`，Cookie 失效后通常只暴露上游 `403 illegal request`，恢复登录需要人工提取 Cookie、改 Zeabur 环境变量并重新部署。
- 理由：运行时 session 持久化可在不重新部署的情况下恢复登录；环境变量回退保持已有部署兼容；显式状态查询和验证缓存降低网易云登录接口的调用频率；统一错误语义避免把所有 403 误判为登录过期。
- 存储与安全边界：SQLite 只保存登录必需的 `MUSIC_U`、`__csrf` 及非秘密元数据。当前单实例个人部署已依赖服务端持久卷和管理面访问控制，因此不新增需要轮换和备份的应用层加密密钥；数据库、WAL 和备份必须按秘密处理，并限制文件权限。日志、MCP 结果和异常不得包含 Cookie、token 或 CSRF。
- 证据：`netease_session.py`、`persistence.py`、`server.py`、`tests/test_server.py`、`README.md` 与 `SECURITY.md` 的当前工作区实现。
- 影响：二维码登录需要既有 `MCP_STORAGE_PATH` 指向持久卷；OAuth 客户端需要 `netease.session` scope；登出会写入 tombstone，阻止旧环境变量 Cookie 立即复活，直到新的二维码登录成功。
- 重新考虑条件：部署模型变为多租户/多实例、持久卷不再具备可信访问边界，或上游二维码/验证协议发生变化。
- 替代：无
- 被替代：无

### DEC-008：web QR context 与短期网页登录共用单次内存 attempt

- 日期：2026-09-28
- 状态：superseded
- 决策：网易云二维码登录使用 web flow，并把 `chainId`、临时 web Cookie、浏览器标识、QR
  key 与检查时间保存在同一个有 10 分钟 TTL 的内存 attempt 中。MCP 与
  `/netease/login/<token>` 网页调用同一检查函数；URL token 由高熵随机值生成，成功、过期或
  安全验证状态后立即失效。浏览器只接收二维码和脱敏状态，最终 session 只在服务端持久化。
- 背景：第一次真实验收中，旧 `type=3` / `/login?codekey=` flow 可以扫码，但确认后返回第一版
  未识别的状态且没有 session。ChatGPT 客户端也没有稳定展示 MCP image content。
- 理由：2026 年仍在维护的 API 实现表明 web QR key/create/check 需要连续的 `type=1`、
  `chainId`、临时 Cookie 与 web headers；短期 capability URL 可在不引入前端项目或第二套状态机
  的前提下，提供稳定的浏览器扫码体验。
- 安全边界：临时 web Cookie、QR key 和 chain context 不写 SQLite、不返回 MCP、不进入日志；
  登录页 token 不允许读取已有 persistent session。`8821` 只报告需要额外安全验证，不尝试绕过。
- 证据：`netease_session.py`、`server.py`、`tests/test_server.py` 与 README 当前工作区实现；协议
  依据为 NeteaseCloudMusicApiEnhanced/api-enhanced 的 PR #201，但本轮没有真实账号网络测试。
- 影响：远程部署继续使用既有 `MCP_PUBLIC_URL` 生成 HTTPS login URL；未新增环境变量、依赖或
  SQLite migration。没有配置 public URL 时仍保留 MCP QR payload / PNG fallback。
- 重新考虑条件：网易云再次改变 web QR 协议，或部署模型变为多实例（内存 attempt 需要共享）。
- 替代：无
- 被替代：DEC-009

### DEC-009：8821 由原网页登录页承接网易官方交互验证

- 日期：2026-09-28
- 状态：active
- 决策：继续使用 DEC-008 的单次 web QR attempt、短期 capability URL 与共享后端检查函数，
  但 `8821` 不再使 attempt 失效。页面暂停普通轮询，通过网易官方 `initNECaptcha` 让用户手动
  完成交互验证，并把 callback 的 `data.validate` 作为单次 `secureCaptcha`；每次请求通过网易
  官方 `createNEFingerprint` 新取 `ydDeviceToken`，再用相同 key、`chainId`、临时 Cookie 与
  User-Agent 恢复检查。`8830` 是当前 flow 不可继续的显式终态。
- 背景：真实 Zeabur 验收证明 DEC-008 的 web QR、网页登录与 `8821` 识别均正确，但把 `8821`
  当终态会阻断所有被网易要求安全验证的账号，无法到达 `803` 和 session 持久化。
- 理由：MaigoLabs/amaoke.app 与 LampTales/cloudmusic2ktv 的公开实现均使用 captcha ID
  `73a18dc827b24b18ad0783701a75277d` 和网易易盾 loader；后者还使用网易设备脚本、app ID
  `9d0ef7e0905d422cba1ecf7e73d77e67`，并在每次 poll 获取新 token。这些常量属于网易当前 web
  登录协议，可能随上游变化，若官方页面或多个维护实现变化必须重新核对，不能自行生成替代值。
- 安全边界：只允许真实浏览器执行网易官方 challenge 和设备脚本；不破解、伪造或重放验证。
  `secureCaptcha`、`ydDeviceToken` 和其值不进日志、MCP、SQLite 或异常文本，只在当前请求与
  10 分钟内存 attempt 的防重放摘要中短暂存在。网页登录页 CSP 只放行所需网易域名。
- 证据：`server.py`、`tests/test_server.py`、`README.md` 与 `SECURITY.md` 当前工作区实现；协议
  参考固定版本的 MaigoLabs/amaoke.app 和 LampTales/cloudmusic2ktv。测试只使用 synthetic/mock
  数据，本轮没有再次调用真实网易账号。
- 影响：未新增依赖、环境变量、数据库表或 migration。官方 SDK 不可用、用户关闭 challenge
  或初始化超时时，attempt 保留到用户重试、明确取消或 TTL 到期；成功、800、8830 与明确取消
  会清理 attempt。
- 重新考虑条件：网易改变 captcha ID、device app ID、SDK origin、challenge callback 或 QR
  risk code；部署模型变为多实例时还需解决 attempt 共享，但不属于本决策范围。
- 替代：DEC-008 中“安全验证状态后立即失效、8821 只报告不承接”的部分；其余 web QR attempt
  与短期 URL 边界继续保留。
- 被替代：无
