# AnyRouter 多账号自动签到

[![AnyRouter 自动签到](https://github.com/moyu929/anyrouter-check-in/actions/workflows/checkin.yml/badge.svg)](https://github.com/moyu929/anyrouter-check-in/actions/workflows/checkin.yml)
[![GitHub Actions](https://github.com/moyu929/anyrouter-check-in/workflows/PR%20Quality%20Checks/badge.svg)](https://github.com/moyu929/anyrouter-check-in/actions)
[![codecov](https://codecov.io/gh/moyu929/anyrouter-check-in/branch/main/graph/badge.svg)](https://codecov.io/gh/moyu929/anyrouter-check-in)
[![pre-commit.ci status](https://results.pre-commit.ci/badge/github/moyu929/anyrouter-check-in/main.svg)](https://results.pre-commit.ci/latest/github/moyu929/anyrouter-check-in/main)
[![Python Version](https://img.shields.io/badge/python-3.11%2B-blue)](https://www.python.org/downloads/)
[![Code style: ruff](https://img.shields.io/badge/code%20style-ruff-000000.svg)](https://github.com/astral-sh/ruff)
[![License](https://img.shields.io/github/license/moyu929/anyrouter-check-in)](LICENSE)

多平台多账号自动签到，支持 **NewAPI** / **OneAPI** 架构平台，内置 `agentrouter`、`gorouter`、`cun`、`gptgod`、`lyclaude`、`nianhua`、`superapi`、`hcnsec`、`guyscode` 等签到分支，其它平台可自定义配置。

**维护开源不易，如果本项目帮助到了你，请帮忙点个 Star，谢谢！**

---

## 1. 项目架构

### 1.1 目录结构

| 路径                            | 职责                                                                                |
| ------------------------------- | ----------------------------------------------------------------------------------- |
| `checkin.py`                    | 入口与编排：加载配置 → 逐账号登录/签到 → 汇总余额变化 → 推送通知 → 决定退出码       |
| `utils/checkin_core.py`         | **签到中枢**：幂等判定、余额换算、用户信息构造、new-api 共享协议、标准签到流程编排  |
| `utils/config.py`               | `ProviderConfig` / `AccountConfig` 数据类，解析 `PROVIDERS` 与 `ANYROUTER_ACCOUNTS` |
| `utils/http_client.py`          | 统一 httpx 客户端工厂（UA、Client Hints、HTTP/2、代理）与指数退避重试               |
| `utils/browser.py`              | CloakBrowser 启动、登录页导航、邮箱表单填写、登录态判定、调试截图                   |
| `utils/popups.py`               | 关闭站点公告弹窗（Playwright 定位 + JS 兜底），并保护登录表单不被误关               |
| `utils/proxy.py`                | 按提供商 `use_proxy` 读取代理、连通性探测与直连回退                                 |
| `utils/proxy_selector.py`       | mihomo REST API 节点选择器：按区域优先级选最低延迟可用节点，维护全局排除集合        |
| `utils/gptgod.py`               | GPTGod 纯 API 分支：`_jztz` 签名算法（仅保留差异逻辑，流程由中枢编排）              |
| `utils/newapi_jwt.py`           | 新版 New-API 站点纯 API 分支（JWT Bearer 认证）                                     |
| `utils/newapi_session.py`       | 老版 New-API 站点纯 API 分支（session cookie + `New-Api-User` 头，支持 CNY 汇率）  |
| `utils/guyscode.py`             | Guyscode 分支：refresh_token 续期 + 浏览器登录捕获 JWT（已搁置，见 2.5）            |
| `utils/notify.py`               | 10 个通知渠道，逐个独立发送、互不阻塞；支持 Markdown 正文与渠道自动适配             |
| `utils/debug.py`                | 统一日志前缀与 `DEBUG_MODE` 开关                                                    |
| `scripts/setup_mihomo_proxy.sh` | CI 内下载 mihomo、生成配置（含 REST API）、启动本地代理并探测节点                  |

### 1.2 执行流程

```
加载 .env / 环境变量
  ↓
AppConfig.load_from_env()      内置 8 个提供商 + PROVIDERS 覆盖
load_accounts_config()         ANYROUTER_ACCOUNTS 校验
  ↓
逐账号 check_in_account()      （use_proxy 账号由 check_in_account_with_retry 包裹节点切换重试）
  ├─ gptgod / guyscode / newapi_jwt / newapi_session
  │     → 各自分支模块（仅差异逻辑）→ checkin_core.run_standard_checkin 统一编排
  ├─ auth_method == "oauth"    → GitHub OAuth 三段式重放（登录即签到）
  ├─ 配置了 email + password   → 浏览器登录取 cookies → HTTP 签到
  └─ 仅配置 cookies            → 补 WAF cookies → HTTP 签到
  ↓
run_check_in_requests()        查余额 → 签到 → 再查余额（WAF 拦截时浏览器兜底）
  ↓
余额哈希比对（balance_hash.txt）+ 自动签到型跨日快照（balance_snapshot.json）
  ↓
失败或余额变化 → notify.push_message()（Markdown 正文）→ sys.exit(0/1)
```

### 1.3 签到分支（按逻辑方式划分）

| 逻辑分类                  | 实现路径                                                  | 是否需要浏览器 | 是否需要签名  | 签到触发  | 适用提供商                          |
| ------------------------- | --------------------------------------------------------- | :------------: | :-----------: | :-------: | ----------------------------------- |
| **浏览器邮箱登录签到**    | 启动浏览器 → 填写邮箱密码 → 获取 cookies → 手动 POST 签到 |       是       |      否       |   手动    | `lyclaude`、自定义 NewAPI 站点      |
| **Session cookies 签到**  | 直接使用用户提供的 cookies → 获取 WAF cookies → 签到      | 仅 WAF 绕过时  |      否       | 手动/自动 | `lyclaude`（Session 模式）、自定义   |
| **GitHub OAuth 重放签到**  | 重放 GitHub OAuth 授权流程 → 签到（登录自动触发或主动接口）|       否       |      否       | 手动/自动 | `agentrouter`、`gorouter`、`cun`    |
| **纯 API 签名签到**       | API 登录 → 获取签名参数 → 生成 `_jztz` → 签到             |       否       | 是（`_jztz`） |   手动    | `gptgod`                            |
| **New-API JWT 签到**      | API 登录换 `access_token` → Bearer 请求 → 主动签到        |       否       |      否       |   手动    | `nianhua`、`superapi`               |
| **New-API Session 签到**  | API 登录种 session cookie + 用户 id 头 → 主动签到         |       否       |      否       |   手动    | `hcnsec`（CNY 汇率显示）            |

### 1.4 签到分支（按提供商划分）

| 提供商        | 认证方式            | 逻辑分类                      | `use_proxy` 默认值 | 关键特性                                    |
| ------------- | ------------------- | ----------------------------- | :----------------: | ------------------------------------------- |
| `agentrouter` | GitHub OAuth        | GitHub OAuth 重放签到         |       `true`       | 无需密码，登录即签到                        |
| `gorouter`    | GitHub OAuth        | GitHub OAuth 重放签到         |      `false`       | 无需密码，登录即签到                        |
| `cun`         | GitHub OAuth        | GitHub OAuth 重放签到         |       `true`       | OAuth 登录 + 主动签到接口，大陆无法直连     |
| `gptgod`      | 邮箱+密码           | 纯 API 签名签到               |      `false`       | 无需浏览器，签名算法反爬，余额单位为积分    |
| `lyclaude`    | 邮箱+密码 / Session | 浏览器登录签到 / Session 签到 |      `false`       | NewAPI 标准，WAF 绕过                      |
| `nianhua`     | 邮箱+密码           | New-API JWT 签到              |      `false`       | 新版 new-api（JWT Bearer），余额单位为美元  |
| `superapi`    | 邮箱+密码           | New-API JWT 签到              |      `false`       | 新版 new-api（JWT Bearer），余额单位为美元  |
| `hcnsec`      | 邮箱+密码           | New-API Session 签到          |      `false`       | 老版 new-api，余额按实时汇率以人民币显示    |
| `guyscode`    | 邮箱+密码           | 纯 API 签到（refresh_token）  |      `false`       | **已搁置**：登录被 Turnstile 强门槛阻断，见 2.5 |

> **代理生效范围**：代理按提供商粒度启用。即使已设置 `CHECKIN_PROXY_URL`，`use_proxy=false` 的提供商仍然直连；可通过 `PROVIDERS` 覆盖每个提供商的 `use_proxy`。

> **签到频率**：每天北京时间 9:00 自动执行一次（GitHub Actions 实际延时约 1~1.5h），可随时手动触发。若当日有账号失败，北京时间 16:00 的**重试工作流**会自动补签失败账号一次（单次尝试，不做节点切换重试）。

---

## 2. 快速开始

### 2.1 Fork 本仓库

点击右上角 "Fork" 按钮。

### 2.2 配置账号

在仓库 Settings → Environments → `production` → **Environment secrets** 中添加 `ANYROUTER_ACCOUNTS`。

#### 2.2.1 各分支配置格式

| 认证方式     | 适用提供商                                                                 | 配置示例                                               | 说明                                   |
| ------------ | -------------------------------------------------------------------------- | ------------------------------------------------------ | -------------------------------------- |
| 邮箱+密码    | `gptgod` / `lyclaude` / `nianhua` / `superapi` / `hcnsec` / `guyscode`     | `{"email":"user@ex.com","password":"pass"}`            | 推荐，自动登录                         |
| Session      | `lyclaude` / 自定义                                                         | `{"cookies":{"session":"xxx"},"api_user":"12345"}`     | 兼容旧版，需手动获取 cookies           |
| GitHub OAuth | `agentrouter` / `gorouter` / `cun`                                          | `{"github_session":"your_github_user_session_cookie"}` | 需提供 GitHub 的 `user_session` cookie |

#### 2.2.2 多账号混合配置示例

```json
[
  {
    "name": "AgentRouter 账号",
    "provider": "agentrouter",
    "github_session": "your_github_user_session_cookie"
  },
  {
    "name": "CUN.AI 账号",
    "provider": "cun",
    "github_session": "your_github_user_session_cookie"
  },
  {
    "name": "GPTGod 账号",
    "provider": "gptgod",
    "email": "user2@example.com",
    "password": "pass2"
  },
  {
    "name": "nianhua 账号",
    "provider": "nianhua",
    "email": "user3@example.com",
    "password": "pass3"
  },
  {
    "name": "hcnsec 账号",
    "provider": "hcnsec",
    "email": "user4@example.com",
    "password": "pass4"
  }
]
```

#### 2.2.3 账号配置字段说明

| 字段             | 类型            | 必填 | 说明                                                                   |
| ---------------- | --------------- | ---- | ---------------------------------------------------------------------- |
| `name`           | string          | 否   | 显示名称，默认 `Account N`；设为空字符串会导致配置校验失败             |
| `provider`       | string          | 否   | 服务商标识，默认 `anyrouter`                                           |
| `email`          | string          | 条件 | 邮箱密码登录时必填                                                     |
| `password`       | string          | 条件 | 邮箱密码登录时必填                                                     |
| `github_session` | string          | 条件 | GitHub OAuth 时必填                                                    |
| `cookies`        | object / string | 条件 | Session 登录时必填，如 `{"session":"xxx"}`，也接受 `"a=1; b=2"` 字符串 |
| `api_user`       | string          | 条件 | Session 登录时必填；邮箱密码与 OAuth 登录会自动获取，可省略            |

> **优先级**：同一账号同时配置了 `github_session`（且提供商为 OAuth）与 `email`/`password` 时，OAuth 优先；两者都没有才回退到 `cookies`。登录失败不会退回使用可能已过期的 `cookies`。

#### 2.2.4 如何获取 Session 与 api_user

仅 Session 模式需要手动获取。登录站点后打开浏览器开发者工具 → Network，任选一个 API 请求：

| 需要的值          | 获取位置                           | 参考截图                                        |
| ----------------- | ---------------------------------- | ----------------------------------------------- |
| `cookies.session` | 请求头 `Cookie` 中的 `session=...` | ![获取 session](./assets/request-session.png)   |
| `api_user`        | 请求头 `new-api-user` 的值         | ![获取 api_user](./assets/request-api-user.png) |


### 2.3 启用 GitHub Actions

1. 进入仓库 **Actions** 选项卡，启用 Workflow
2. 可手动触发 **Run workflow** 测试，勾选 `debug` 可开启调试日志

![运行结果](./assets/check-in.png)

### 2.4 GPTGod 分支前置条件

`gptgod` 分支不使用浏览器，签名链路自包含：

- **设备指纹**：内置一份固定的设备身份指纹（所有账号共享），仅单次访问行为参数随机化。长期稳定的指纹更不易被风控识别为异常。
- **签名参数**：每次运行从 `/api/user/register-config` 实时拉取并解密，无需手动维护。若对方更换算法（重排表长度不再是 33 或 XOR 密钥不再是 16），签到会失败并在日志中提示。

### 2.5 Guyscode 分支（已搁置）

`guyscode` 的签到链路（refresh_token 续期 → 查余额 → 签到）已实现，但其登录接口强制校验 **Cloudflare Turnstile** 人机验证，CloakBrowser 指纹无法通过验证框渲染，自动登录被阻断。若需恢复：

1. 用真人浏览器手动登录一次，导出 `refresh_token` 到 `guyscode_refresh.json`，验证其能否跨天存活；
2. 可跨天则纯 API 无人值守签到成立（仅需一次手动登录）；否则需接入 Turnstile 代过服务（对免费 API 站不划算）。

**注意**：`guyscode_refresh.json` 含凭据，已在 `.gitignore` 中忽略，勿提交。

---

## 3. 自定义提供商

通过环境变量 `PROVIDERS` 配置其他 NewAPI/OneAPI 平台。自定义配置会与同名内置提供商合并——未指定的字段沿用内置默认值。

### 3.1 配置字段

| 字段                  | 类型           | 必填 | 默认值              | 说明                                                                                          |
| --------------------- | -------------- | ---- | ------------------- | --------------------------------------------------------------------------------------------- |
| `domain`              | string         | 是   | —                   | 服务商域名，如 `https://example.com`                                                          |
| `login_path`          | string         | 否   | `/login`            | 登录页面路径                                                                                  |
| `sign_in_path`        | string \| null | 否   | `/api/user/sign_in` | 签到 API 路径（设为 `null` 则查用户信息时自动签到）                                           |
| `user_info_path`      | string         | 否   | `/api/user/self`    | 用户信息 API 路径                                                                             |
| `api_user_key`        | string \| null | 否   | `new-api-user`      | 用户标识请求头名称；设为 `null` 则不发送该请求头                                              |
| `bypass_method`       | string \| null | 否   | `null`              | WAF 绕过方式：`"waf_cookies"` 或 `null`                                                       |
| `waf_cookie_names`    | array          | 条件 | —                   | WAF 绕过所需 cookie 名称列表；为空或全部非法时 `bypass_method` 自动降级为 `null`              |
| `use_proxy`           | bool           | 否   | `false`             | 该提供商是否走代理                                                                            |
| `persist_profile`     | bool           | 否   | `false`             | 是否复用持久化浏览器 Profile（内置 `anyrouter` 为 `true`），可减少重复登录与风控              |
| `auth_method`         | string \| null | 否   | `null`              | `"oauth"` 走 GitHub OAuth 重放，`"gptgod"` 走 GPTGod 纯 API 分支；`null` 时按账号字段自动判断 |
| `oauth_client_id`     | string         | 条件 | —                   | `auth_method="oauth"` 时必填                                                                  |
| `oauth_state_path`    | string         | 否   | `/api/oauth/state`  | OAuth 第 1 步取 state 的接口路径                                                              |
| `oauth_callback_path` | string         | 否   | `/api/oauth/github` | OAuth 第 3 步回调接口路径                                                                     |

> `auth_method` 的字面量中还包含 `"email"`，但当前代码没有为它单独分支——邮箱密码登录是在账号同时配置了 `email` 与 `password` 时自动走的，无需显式设置。

### 3.2 配置示例

```json
{
  "customrouter": {
    "domain": "https://custom.example.com",
    "sign_in_path": "/api/checkin",
    "bypass_method": "waf_cookies",
    "waf_cookie_names": ["acw_tc", "cdn_sec_tc"],
    "use_proxy": true
  }
}
```

> 在仓库 Settings → Environments → `production` → **Environment secrets** 中添加 `PROVIDERS`。

### 3.3 通配符 `*`：一键控制全部提供商代理开关

`PROVIDERS` 支持保留字 `"*"`，其中声明的字段会统一应用到所有内置提供商——最典型的用法是一键全代理或全直连：

```json
{
  "*": { "use_proxy": true }
}
```

```json
{
  "*": { "use_proxy": false }
}
```

优先级为 **内置默认 < `"*"` 通配 < 具体提供商条目**。即：通配符覆盖所有内置默认，但具体条目可以再覆盖通配符：

```json
{
  "*": { "use_proxy": true },
  "gptgod": { "use_proxy": false }
}
```

上例表示：除 gptgod 直连外，其余提供商全部走代理。工作流的 `NEEDS_PROXY` 判断（是否初始化 mihomo）遵循同一优先级。

---

## 4. 环境变量总览

### 4.1 账号与提供商

| 变量                 | 默认值  | 说明                                                             |
| -------------------- | ------- | ---------------------------------------------------------------- |
| `ANYROUTER_ACCOUNTS` | —       | **必填**，账号配置 JSON 数组，见 2.2                             |
| `PROVIDERS`          | —       | 自定义提供商 JSON 对象，见第 3 节                                |
| `DEBUG_MODE`         | `false` | 调试模式，见第 7 节                                              |
| `RETRY_TIMES`        | `3`     | HTTP 请求失败重试次数（5xx / 429 / 网络异常），设为 `0` 禁用重试 |

### 4.2 浏览器行为

仅影响需要浏览器的分支（`anyrouter` 邮箱登录、WAF cookies 获取）。

| 变量                           | 默认值                  | 说明                                                                         |
| ------------------------------ | ----------------------- | ---------------------------------------------------------------------------- |
| `CHECKIN_HEADLESS`             | `true`                  | 是否无头运行。CI 中设为 `false` 并用 `xvfb-run` 提供虚拟显示，以降低无头特征 |
| `CHECKIN_HUMANIZE`             | `true`                  | 是否启用 CloakBrowser 拟人化操作                                             |
| `CHECKIN_HUMANIZE_AGENTROUTER` | 跟随 `CHECKIN_HUMANIZE` | 仅覆盖 `agentrouter` 的拟人化开关                                            |
| `CHECKIN_WAIT_TIMEOUT_MS`      | `60000`                 | 登录等待超时（毫秒）。CI 中设为 `120000`                                     |
| `CHECKIN_BROWSER_PROFILE_DIR`  | `.browser_profiles`     | 持久化 Profile 根目录，实际路径为 `<根目录>/<提供商>/<账号名>`               |
| `CHECKIN_SCREENSHOT_DIR`       | `checkin_screenshots`   | 调试截图输出目录                                                             |
| `CLOAKBROWSER_BINARY_PATH`     | —                       | 指向本地浏览器可执行文件，跳过 `cloakbrowser install`                        |

> 布尔类变量接受 `1` / `true` / `yes` / `on`（不区分大小写），其余值均视为 false。

### 4.3 代理

| 变量                     | 默认值                                 | 作用位置          | 说明                                                                                |
| ------------------------ | -------------------------------------- | ----------------- | ----------------------------------------------------------------------------------- |
| `PROXY_SUBSCRIPTION_URL` | —                                      | CI 脚本           | Clash/Mihomo 订阅链接。**未设置则整个代理步骤跳过**                                 |
| `CHECKIN_PROXY_URL`      | —                                      | Python            | 代理地址，如 `http://127.0.0.1:7890`。CI 中由代理脚本自动写入 `GITHUB_ENV`          |
| `PROXY_TEST_URL`         | `https://www.gstatic.com/generate_204` | Python 与 CI 脚本 | 连通性探测地址。CI 中显式设为 `https://www.google.com/generate_204`，两侧默认值不同 |
| `PROXY_PORT`             | `7890`                                 | CI 脚本           | mihomo 本地 mixed-port                                                              |
| `MIHOMO_VERSION`         | `v1.19.0`                              | CI 脚本           | mihomo 版本。当前 workflow 固定为 `v1.19.27`                                        |
| `PROXY_REQUIRED`         | `false`                                | CI 脚本           | `true` 时代理不可用即让 workflow 失败退出；`false` 时降级为直连继续执行             |
| `PROXY_RETRY_TIMES`      | `3`                                    | Python            | 代理节点问题（WAF/5xx/超时）时，切换节点重试的最多次数，含最后一次直连兜底         |
| `MIHOMO_CONTROLLER`      | —                                      | Python            | mihomo REST API 地址，由代理脚本自动写入 `GITHUB_ENV`，无需手动设置                |
| `MIHOMO_CONTROLLER_PORT` | `9090`                                 | CI 脚本           | mihomo REST API 端口，可覆盖默认值                                                  |
| `MIHOMO_SECRET_FILE`     | —                                      | Python            | mihomo REST API secret 文件路径，由代理脚本自动写入 `GITHUB_ENV`，无需手动设置     |
| `PROXY_NODE_DELAY_TIMEOUT_MS` | `3000`                            | Python            | 节点测延迟超时（毫秒），测速超时/失败的节点会被排除，然后选最低延迟可用节点         |

### 4.4 通知

见第 6 节。

---

## 5. 代理配置

当 GitHub Actions IP 被 WAF 屏蔽或不稳定时，可配置代理。只需设置 `PROXY_SUBSCRIPTION_URL`，workflow 会自动下载 mihomo、生成配置、启动本地代理，并把地址写入 `CHECKIN_PROXY_URL`。

首次启动前会检测是否存在 `use_proxy=true` 的账号，若全部账号均不使用代理，则跳过整个代理初始化流程。

### 5.1 节点选择器

代理节点由 Python 节点选择器主动控制，不再依赖 mihomo 自动测速切换，避免中途切换影响进行中的签到任务。

选择逻辑如下：

```
1. 🇯🇵 日本 组内并行测速 → 选延迟最低节点 → 连通性验证 → 切换
   ├─ 不通 → 排除该节点 → 选组内次低延迟节点 → 重试
   └─ 组内全部排除 → 进入 🇸🇬 新加坡
2. 🇸🇬 新加坡 组内同逻辑
   └─ 全部排除 → 进入 🇭🇰 香港
3. 🇭🇰 香港 组内同逻辑
   └─ 全部排除 → 返回 None（该账号尝试直连兜底）
```

- 节点选择器维护**全局排除集合**，一旦某节点因 WAF 拦截 / 超时 / 连通性失败被排除，后续所有账号与重试不再选择该节点。
- 选择过程包含完整日志，用户可通过日志跟踪节点切换过程。

### 5.2 签到重试

使用代理的账号签到遇到以下问题时，自动切换节点重试（最多 3 次）：

- 阿里云 WAF 拦截（响应体含 `aliyun_waf_aa`）
- 5xx 服务端错误
- 网络异常（超时 / 连接断开 / DNS 解析失败）
- 节点连通性验证失败

若节点选择器已无可用节点，最后一次尝试**直连目标网站**。重试仍失败则放弃该账号，继续下一账号。

### 5.3 回退机制

```
🇯🇵 日本 → 🇸🇬 新加坡 → 🇭🇰 香港 → 直连
```

- **节点选择器**：按区域顺序，区域内并行测速选最低延迟节点，逐节点验证连通性，不通则排除并换下一个。
- **Python 层**：进程内首次用到代理时做一次连通性探测并缓存结果；探测失败则该次运行全程直连，并在日志中打印 `代理 ... 不可达，回退到直连`。
- **提供商层**：`use_proxy=false` 的提供商不读取代理地址、不做探测，始终直连。

> 代理仅作用于本项目的浏览器与 HTTP 客户端（通过 `CHECKIN_PROXY_URL`），不会设置全局 `HTTP_PROXY`/`HTTPS_PROXY`，因此不影响 Actions 的其他步骤。

---

## 6. 通知方式

支持多通道同时推送，只需配置对应的环境变量即可。每个通道独立发送，单个通道失败不会影响其他通道。

| 通知方式           | 所需环境变量                              | 说明                                                                                                    |
| ------------------ | ----------------------------------------- | ------------------------------------------------------------------------------------------------------- |
| **邮箱 (SMTP)**    | `EMAIL_USER` + `EMAIL_PASS` + `EMAIL_TO`  | SMTP 服务器默认由邮箱域名推导（`smtp.<域名>`，465 端口 SSL）。可选 `EMAIL_SENDER`、`CUSTOM_SMTP_SERVER` |
| **钉钉机器人**     | `DINGDING_WEBHOOK`                        | 群机器人 Webhook 地址                                                                                   |
| **飞书机器人**     | `FEISHU_WEBHOOK`                          | 群机器人 Webhook 地址                                                                                   |
| **企业微信机器人** | `WEIXIN_WEBHOOK`                          | 群机器人 Webhook 地址                                                                                   |
| **PushPlus**       | `PUSHPLUS_TOKEN`                          | [PushPlus 官网](http://www.pushplus.plus)                                                               |
| **Server 酱**      | `SERVERPUSHKEY`                           | [Server 酱官网](https://sct.ftqq.com)                                                                   |
| **Telegram Bot**   | `TELEGRAM_BOT_TOKEN` + `TELEGRAM_CHAT_ID` | Telegram Bot Token 和 Chat ID                                                                           |
| **Gotify**         | `GOTIFY_URL` + `GOTIFY_TOKEN`             | 自建 Gotify 服务。可选 `GOTIFY_PRIORITY`（自动裁剪到 1-10，默认 9）                                     |
| **Bark**           | `BARK_KEY`                                | iOS Bark 推送。可选 `BARK_SERVER`（默认 `https://api.day.app`）                                         |
| **NotifyX**        | `NOTIFYX_KEY`                             | [NotifyX 多通道推送](https://www.notifyx.cn/help)。可选 `NOTIFYX_TEAM`（群组 ID）                        |

> **日志说明**：未配置任何渠道时只打印一行 `[通知] 未配置任何通知渠道，跳过推送`；已配置的渠道发送失败会在日志中打印 `[警告] <渠道> 推送失败: ...`。这是预期输出，不代表签到失败——只要有任一通道配置正确并发送成功（`[通知] <渠道> 推送成功`）即可。

> **通知触发条件**：仅在「有账号签到失败」「检测到余额变化」或「签到成功但余额未获取」时推送；全部成功且余额无变化时跳过通知。余额指纹保存在 `balance_hash.txt`（CI 中通过缓存跨运行保留），因此首次运行必定推送一次。

#### 6.1 通知正文格式（Markdown）

通知正文为 Markdown 格式，每个账号带序号加粗，字段收敛为「签到前余额 / 签到获得 / 签到后余额 / 累积消耗」，签到失败时显示失败原因：

```
执行时间: 2026-08-25 09:12:34

**1. gptgod-1** ✅
签到前余额: 610635 积分
签到获得: +2000 积分
签到后余额: 612635 积分
累积消耗: 0 积分

**2. agentrouter** ✅ 签到成功
余额未获取: 余额查询失败（WAF/网络异常）: aliyun_waf_aa...

**3. lyclaude** ❌
失败原因: OAuth 登录失败（详见运行日志）

**📊 统计**
成功: 2/3，失败: 1/3
部分账号签到成功 ⚠️
```

- 已签到且余额无变化的账号只显示「当前余额 + 累积消耗」，不重复展示前后对比
- 自动签到型（登录即签到）展示「跨日估算奖励 + 当前余额 + 累积消耗」
- 各渠道自动适配：NotifyX / 飞书直接渲染 Markdown；Telegram 转为 HTML 粗体；钉钉、企业微信、Bark 等纯文本渠道自动去掉 Markdown 标记

> **注意**：若 webhook 有安全要求（如钉钉），可在机器人安全设置中选择**自定义关键词**，填写 `AnyRouter`。

---

## 7. 调试模式

在仓库 Settings → Environments → `production` → **Environment variables** 中添加 `DEBUG_MODE=true`，或手动触发 workflow 时勾选 `debug`：

- 输出请求 URL、响应状态码、重试过程与 cookie 名称（**不打印响应体与 cookie 值**，避免泄露凭据）
- 保存浏览器登录关键节点截图，并上传为 Actions Artifact `checkin-screenshots-<run_id>`
- 输出代理端点、各提供商 `use_proxy` 取值、`api_user`、浏览器 Profile 路径等信息

---

## 8. 退出码

| 退出码 | 含义                                                   |
| :----: | ------------------------------------------------------ |
|  `0`   | 至少一个账号签到成功                                   |
|  `1`   | 全部账号签到失败、账号配置加载失败、或运行时未捕获异常 |

> 多账号场景下，只要有一个成功就返回 `0`，Actions 显示为绿色；失败账号的详情通过通知与日志体现。

---

## 9. 本地开发

```bash
# 安装依赖
uv sync --dev

# 安装 CloakBrowser 浏览器
uv run python -m cloakbrowser install

# 配置 .env 文件（参考 .env.example）
# 运行签到
uv run checkin.py

# 运行测试
uv run pytest tests/ --cov=.
```

### 9.1 代码质量

| 工具       | 用途                 | 命令                                           |
| ---------- | -------------------- | ---------------------------------------------- |
| Ruff       | 代码风格检查与格式化 | `uv run ruff check .` / `uv run ruff format .` |
| MyPy       | 静态类型检查         | `uv run mypy .`                                |
| Bandit     | 安全漏洞扫描         | `uv run bandit -r . -c pyproject.toml`         |
| Pytest     | 自动化测试           | `uv run pytest tests/ --cov=.`                 |
| pre-commit | 提交前自动检查       | `uv run pre-commit install`                    |

> 代码风格：行宽 120、单引号、**Tab 缩进**。`pyproject.toml` 中 `fix = true`，`ruff check .` 会自动修改文件；只想查看问题请加 `--no-fix`。

> PR 检查中 Ruff Lint、Ruff Format、Pytest 失败会阻止合并；MyPy 与 Bandit 仅告警。

### 9.2 测试约定

测试全部离线运行，不访问任何真实站点、不使用真实账号：

- HTTP 分支用 `httpx.MockTransport` 拦截；浏览器分支用鸭子类型的假 Page / Context / Locator
- 涉及 `time.sleep` 的重试与轮询均被 monkeypatch 掉，保证测试秒级完成
- 测试数据使用 `example.invalid` 等保留域名，避免误发真实请求

---

## 10. 故障排除

| 现象                            | 可能原因                     | 解决方法                                                                                |
| ------------------------------- | ---------------------------- | --------------------------------------------------------------------------------------- |
| 401 错误                        | cookies 过期                 | 重新获取 cookies 或改用邮箱密码登录                                                     |
| 1040 (08004)                    | 服务商数据库连接数超限       | 等待后重试，官方问题                                                                    |
| WAF 拦截（日志含 `aliyun_waf`） | GitHub Actions IP 被屏蔽     | 配置代理（`PROXY_SUBSCRIPTION_URL`）                                                    |
| 签到成功但积分未变              | 今日已签到                   | 检查日志 `已签到` 状态                                                                  |
| 浏览器启动失败                  | CloakBrowser 未安装          | `uv run python -m cloakbrowser install`，或用 `CLOAKBROWSER_BINARY_PATH` 指向本地浏览器 |
| 日志出现 `<渠道> 推送失败`       | 个别通知渠道配置错误         | 属正常输出，只要有一个渠道成功（`推送成功`）即可                                        |
| 每次运行都收到通知              | `balance_hash.txt` 未保留    | CI 依赖缓存保存余额指纹，缓存失效时会被判为首次运行                                     |
| OAuth 登录失败（401/403）       | GitHub `user_session` 已失效 | 重新从浏览器复制 `user_session` cookie                                                  |

---

## 11. 免责声明

本脚本仅用于学习和研究目的，使用前请确保遵守相关网站的使用条款。

