# 项目状态

最后更新：2026-08-13
状态维护者：项目协作者

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
