# 外部定时器对接方案（cron-job.org → repository_dispatch）

> 目的：让每日流水线**准点必跑**。GitHub 自带的 `schedule` 定时是尽力而为：
> 官方说明高负载时会延迟、过载时排队任务会被丢弃。本仓库实测——2026-11-09
> 延迟到 15:18 才启动、11-10 延迟到 19:05、11-11 的 08:41 直到 09:38 仍未触发。
> 改用外部定时器在 08:41 精确调用 GitHub 的 **repository_dispatch API**：
> API 触发不走调度队列，运行通常在 1 分钟内启动。

工作流侧已就绪（`.github/workflows/daily-update.yml`）：

- 已加触发器 `repository_dispatch: types: [daily-update]`；原 `schedule`
  （08:41 / 11:07 两档）与 `workflow_dispatch` 保留作兜底和手动入口。
- 已加失败自动告警（审计 P2-2）：运行硬失败、或收益步骤
  （`fetch_returns.py`，按设计失败不阻断主数据）失败时，自动在本仓库建
  issue《每日数据更新异常 YYYY-MM-DD》（同日同名去重），含失败说明与运行链接。

下面两步需要仓库主账号操作一次（约 5 分钟），完成后每日自动运行。

---

## 步骤一：创建细粒度令牌（PAT）

GitHub 网页：右上角头像 → **Settings** → 左栏最下 **Developer settings** →
**Personal access tokens** → **Fine-grained tokens** → **Generate new token**：

| 字段 | 填法 |
|---|---|
| Token name | `dingtou-daily-dispatch` |
| Expiration | 选最长（Custom，约 1 年）；**到期前需重建并更新步骤二里的令牌**，到期日记在日历上 |
| Resource owner | CPA101 |
| Repository access | **Only select repositories** → 只勾 `dingtou-20nian` |
| Permissions → Repository permissions | **Contents: Read and write**（repository_dispatch 的硬性要求）；**Actions: Read and write**（备用 workflow_dispatch 口需要） |

点 **Generate token**，复制生成的令牌（只显示一次，先存好，下一步要用）。

## 步骤二：在 cron-job.org 建任务

注册并登录 cron-job.org（免费版够用）→ **Create cronjob**：

| 字段 | 填法 |
|---|---|
| Title | `dingtou daily-update 08:41 BJ` |
| URL | `https://api.github.com/repos/CPA101/dingtou-20nian/dispatches` |
| Schedule | 选 **Every week** / 自定义表达式 `41 8 * * 1-5`；**Timezone 设 Asia/Shanghai**，即工作日 08:41 |
| Request method | `POST` |
| Headers | `Accept: application/vnd.github+json` |
|  | `Authorization: Bearer <步骤一的令牌>` |
|  | `X-GitHub-Api-Version: 2022-11-28` |
|  | `Content-Type: application/json` |
| Request body | `{"event_type":"daily-update"}` |
| 通知 | 打开失败通知（Execution history 里状态码应为 **204** 即成功） |

> 兜底任务（已建）：再建一个同样的任务作保底——Title `dingtou daily-update 11:07 BJ backup`，表达式改 `7 11 * * 1-5`，Request body 改为 `{"event_type":"daily-update","client_payload":{"slot":"backup"}}`，其余字段与主任务完全相同。工作流有「保底闸门」：带 `slot=backup` 的触发（以及 GitHub 自带定时两档，2026-11-12 起扩围）会先核对 data/latest.json 与 data/returns.json 的更新日期是否均为今天，均为今天说明主档已成功，本轮整轮跳过、不产生提交；任一不是今天才真正补跑。

> 备用口（如改走 workflow_dispatch）：URL 换成
> `https://api.github.com/repos/CPA101/dingtou-20nian/actions/workflows/daily-update.yml/dispatches`，
> body 换成 `{"ref":"main"}`，其余相同（需要 Actions 写权限，步骤一已包含）。

## 验证（建完点一次 cron-job.org 的「Run now / Test」）

1. cron-job.org 执行历史：状态码 **204**；
2. GitHub 仓库 → Actions：1 分钟内多出一次运行，触发方式显示
   **repository_dispatch**；
3. 该运行成功后，`data/latest.json` 的 `updated_at_bj` 为当次时间。

## 故障排查

| 现象 | 原因 |
|---|---|
| 401 | 令牌错误或已过期 → 重建令牌并更新 cron-job.org |
| 404 | 令牌没勾到本仓库、或 Contents 权限不足 → 回步骤一检查 |
| 204 但 Actions 无新运行 | body 里的 `event_type` 与工作流 `types: [daily-update]` 不一致 |
| 任务跑了但数据没更新 | 看 Actions 运行日志；若工作流失败，仓库 Issues 里应有当日的《每日数据更新异常》告警 |

## 安全说明

- 令牌只授权这一个仓库的 Contents / Actions 读写，拿不到其它仓库和账号设置；
- 令牌只保存在 cron-job.org 的任务配置里，不要写进本仓库任何文件、不要发聊天；
- 怀疑泄露：GitHub Settings → Fine-grained tokens 里 Revoke 后重建，再更新 cron-job.org。
