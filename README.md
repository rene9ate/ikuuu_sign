# iKuuu Checkin

iKuuu VPN 每日自动签到，GitHub Actions 定时执行。

## 工作流程

**两阶段设计：**

1. **Cookie 预检** — 用缓存的 cookie 直接 requests 签到（快，不启动浏览器）
   - 多账号线程池并行执行
   - 网络异常时自动重试 1 次
   - Cookie 过期自动清理

2. **Playwright 浏览器登录**（cookie 无效时）— 共享浏览器，async 并行登录
   - 极验 Geetest V4 验证码处理
   - 支持多个故障域名切换
   - 登录成功自动保存 cookie，下次跳过浏览器登录
   - 登录失败自动切换备用域名

## 环境变量

| 变量 | 说明 |
|------|------|
| `ACCOUNTS` | 账号密码，每行 `邮箱:密码`，多账户换行分隔 |
| `PLAYWRIGHT_HEADLESS` | 默认 `1`（无头），设为 `0` 调试 |

## GitHub Actions

每天 UTC 16:00（北京时间 00:00）自动执行。

需配置 Secrets：`ACCOUNTS`

创建 Issue #1 可在全部签到失败时接收通知。

## 保活机制（Keepalive）

GitHub 会在仓库**连续 60 天没有任何提交活动**时自动停用 scheduled workflow，
而 workflow 的运行本身**不算**仓库活动。因此一个只靠定时跑、长期不提交代码的仓库，
定时任务会在某天静默停摆 —— 本仓库就曾因此被停用（`disabled_inactivity`）。

解决办法是**自维持心跳**：签到任务在每次运行末尾检查距上一次提交的天数，
超过阈值就提交一次 `.github/heartbeat.txt`。这样「签到 → 产生提交 → 仓库有活动 →
定时任务不会被停 → 继续签到」形成闭环，无需任何外部服务。

要点：

- 心跳 step 带 `if: always()`，即使签到失败也会执行
- 仅在距上次提交超过 `HEARTBEAT_MAX_AGE_DAYS`（默认 20 天）时才提交，日常开发时零噪音
- 由 `GITHUB_TOKEN` 产生的提交不会触发新的 workflow 运行，无递归风险
- 阈值远小于 60 天，留有 40 天缓冲

如需调整频率，修改 `checkin.yml` 顶层 `env` 中的 `HEARTBEAT_MAX_AGE_DAYS`。

### 若定时任务已被停用

推送一次提交通常会自动重新激活。若没有，到
`Actions → iKuuu Checkin` 页面点击 **Enable workflow**，或用 API：

```bash
curl -X PUT -H "Authorization: Bearer <TOKEN>" \
  https://api.github.com/repos/rene9ate/ikuuu_sign/actions/workflows/checkin.yml/enable
```

## 本地运行

```bash
pip install -r requirements.txt
playwright install chromium

$env:ACCOUNTS="user@example.com:password"
python playwright_checkin.py
```

## 文件

- `playwright_checkin.py` — 主脚本
- `.github/workflows/checkin.yml` — GitHub Actions 配置
- `requirements.txt` — Python 依赖
- `ikuuu_cookies.json` — Cookie 缓存（自动维护）
- `.github/heartbeat.txt` — 保活心跳记录（自动生成，请勿手改）
