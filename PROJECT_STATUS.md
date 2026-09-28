# 项目状态

最后更新：2026-09-28
状态维护者：项目协作者

## 2026-09-28 web QR 协议兼容与浏览器登录 checkpoint

- 第一次真实 Zeabur 验收证明 OAuth、SQLite、QR key、PNG 与扫码入口可用，但确认后的未知 QR
  code 被第一版折叠为通用错误，session 未持久化。本轮只做本地实现和 mock/synthetic 验证，
  未再次使用真实网易云账号，也未 commit、push 或 deploy。
- QR flow 已切换为 web 语义：`type=1`、`noCheckToken`、`chainId`、web headers、临时浏览器
  Cookie 和 `/st/platform/scanlogin` URL 在同一 10 分钟 attempt 内连续使用。`803` 合并初始
  context、响应 `Set-Cookie` 与 body cookie 后，仍只把 `MUSIC_U` / `__csrf` 存入 SQLite。
- `start_netease_qr_login` 现在优先返回基于既有 `MCP_PUBLIC_URL` 的短期高熵 `login_url`；
  简单 HTML 页面显示 QR 并以 2.5 秒间隔调用与 MCP 相同的检查函数。成功、过期及 `8821`
  会使 URL 失效；access log 隐去 URL token，浏览器不接收最终 Cookie。
- QR 状态明确支持 800/801/802/803/8821；未知数字 code 返回脱敏的
  `upstream_unknown`，不再丢失诊断信息。`8821` 只报告
  `security_verification_required` 并停止轮询，不实现安全验证绕过。
- 本地完整 mock 测试当前 111/111 通过；仓库环境检查、Python compile、`pip check` 与
  `git diff --check` 均通过。旧 `NETEASE_COOKIE` fallback、session manager、OAuth scope、
  SQLite schema 和原音乐工具没有重设计。

## 2026-09-28 网易云登录态自动管理 checkpoint

- canonical working tree：`repo_checkout`，分支 `main`，任务开始时 HEAD `2d72f141e2337d2f4539cb846dc32ffe8d5ecfca`，origin 为 `Rainlxyl/netease-music-mcp-safe`。未修改 `upstream_reference` 或项目外壳目录。
- 已在当前工作区实现统一 session manager、SQLite runtime session、环境变量兼容回退、二维码登录、登出、登录状态工具、CSRF 统一解析与认证错误分类；本轮没有部署或 push。
- 当前工具边界为 16 个音乐读取工具、4 个登录态工具、12 个可选写入工具。默认只读策略和 single-call write 保护保持不变。
- SQLite schema 在现有初始化流程中自动创建 `netease_session` 单例表，不需要手工 migration。二维码登录依赖既有 `MCP_STORAGE_PATH` 指向持久卷；未新增环境变量。
- 安全边界：仅持久化 allowlist 中的 `MUSIC_U` 与 `__csrf`；数据库、WAL 与备份视为秘密；MCP 返回、日志和异常不返回 Cookie/CSRF/token。当前个人单实例部署不新增应用层加密密钥，详见 `DEC-007` 和 `SECURITY.md`。
- 本地自动化使用 mock 覆盖 session 优先级、持久化、CSRF、错误分类、二维码全部状态、确认后保存、MCP PNG image content、OAuth scope 兼容、旧数据库数据保留、登出与秘密不泄露。2026-09-28 当前工作区完整测试 102/102 通过，仓库 `check_dev_environment.ps1`、Python compile、`pip check` 与 `git diff --check` 通过。真实网易云账户、二维码在线接口、Zeabur runtime 与 OAuth 回调仍未在本轮验证，不得把本地测试描述为线上验收。
- 既有 `README.md` 未提交 attribution 修改是在其当前工作区版本上合并保留；3 份 `CODEX_FOR_OSS_*` 未跟踪文档未删除、未移动、未纳入本次实现。
- 下一步：Rain 审阅本地 commit 后，再单独决定是否 push 与部署。

## 2026-08-31 continuity refresh

- continuity closeout 前的 source baseline：`main` at `6726a1e2b41d`；除本文件外，`README.md` modified，3 份 `CODEX_FOR_OSS_*` Markdown untracked，0 staged。doc-only closeout 后以 Git 当前 HEAD 为准，这四份 active docs 必须继续 dirty。
- 当前 active work 是 docs-only，但包含两个可分离主题：README 的 upstream/version attribution；Codex for OSS readiness/evidence/application draft。不得作为一个 mixed commit 处理。
- README attribution 目前只到 **implemented in working tree**；没有提交或单独 acceptance 记录。
- OSS 三份文档是 2026-08-13 的审计/草稿证据，application 明确 `DRAFT ONLY — NOT SUBMITTED`。其中 stars、forks、form fields 等时间敏感主张本轮未联网复核，当前只能视为 historical snapshot。
- 当前产品代码、tests、真实 NetEase API、OAuth、deployment/runtime 均未在本轮重验。v0.1.0 与 88/88 测试只能按下文的 historical verified 边界引用。
- 唯一下一步：Rain 先审阅 README attribution 的准确性和三份 OSS 草稿是否仍要保留；之后再分别决定两个 exact-file commit boundary。不要提交申请或扩大到产品新功能。

以下 2026-08-13 内容保留为发布后维护与审计历史；与本节冲突时，以当前 Git/diff 和本节为准。

## 当前阶段

项目已完成首次公开发布，处于 **v0.1.0 发布后的维护阶段**。截至 2026-08-13 的公开状态复核，
GitHub `main`、本地审计开始前的 `main` 与正式 Release `v0.1.0` 均指向
`beb6fa55c7bb0f500e9f0222c493b62fda0b96c5`（`docs: prepare project for public sharing`）。
本地工作树在本次文档审计开始前为 clean；这里不把后续未提交文档改动描述为已发布。

当前产品提供 **16 个 read tools / 12 个 write tools**。默认 `MCP_READ_ONLY=true`；写入采用
single-call flow，并保留服务端校验、审计、幂等、失败分类和有限撤销能力。

## 2026-08-07 checkpoint（历史记录）

- 修复 OAuth 授权页和封面错误信息中的 stale preview 用户可见文案；没有重新引入 preview flow。
- 发布产品化 `README.md`，明确当前能力、未来设想、历史在线验证与测试证据边界。
- 复核并保留默认只读、OAuth/PKCE、single-call writes、idempotency、podcast ID、timezone 和安全维护决策。
- 将 5 个不适合公开提交但有保留价值的旧 Markdown 移到
  `D:/Codex-projects/netease-music-mcp-safe-local-archive/2026-08-07/`，保留原相对目录结构并逐文件通过 SHA-256 校验：
  `EXPERIMENTS.md`、`HANDOFF.md`、`docs/INCIDENT_TEMPLATE.md`、
  `docs/LESSONS_LEARNED.md`、`docs/PRODUCT_README_DRAFT.md`。

该次 checkpoint 提交主题：

1. `fix: remove stale write-preview wording`
2. `docs: publish product-focused README`
3. `docs: add project continuity checkpoint`

## 验证证据

| 日期 | 检查 | 结果 | 证据边界 |
|---|---|---|---|
| 2026-08-07 | `python -m unittest discover -s tests -v` | 88/88 通过 | 网络调用使用 mock；不证明网易云未公开接口持续在线可用 |
| 2026-08-07 | Python syntax compile | 通过 | 只验证当前 Python 源文件可编译 |
| 2026-08-07 | `git diff --check` | 通过 | 只检查 Git 差异中的空白错误 |
| 2026-08-07 | tool registry 与 README 对照 | 16 read / 12 write，名称一致 | 静态注册表和本地加载结果 |

## 尚未验证

- 本轮未使用真实网易云 Cookie 或真实账户重新调用在线接口。
- 本轮未重新验证 Zeabur runtime、域名、OAuth 回调或持久卷状态。
- README 中 2026-07-30 的 podcast 真实账号/线上部署结果是历史验证，不是持续 SLA。

## 后续可能工作

- 如项目继续运营，可基于公开边界重新设计精简的 incident template；当前归档版本不进入公开仓库。
- 持续观察未公开网易云接口的字段和稳定性，但不把 future work 描述为当前能力。
- 任何新功能开始前，先从本文件、`DECISIONS.md`、当前 Git HEAD 与 clean worktree 恢复上下文。
