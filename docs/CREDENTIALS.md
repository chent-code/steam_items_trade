# 全自动化凭据清单（容易记混，查这一份）

> 最后核对：2026-11-15。项目里前后出现过 4 种都被称为「key / 令牌」的凭据，
> 分别属于不同平台、不同用途、不同寿命。做全自动化前先对照这张表。

## 一、你问的那两个

### 1. BUFF 侧要的「KEY」= **Steam Web API Key**（32 位十六进制字符串）

- **名字**：Steam Web API Key，Steamauto 配置里叫 `api_key` / "Steam 网页 API 密钥"
- **在哪拿**：浏览器登录 Steam → `https://steamcommunity.com/dev/apikey` → 填一个域名（个人用填 `localhost` 即可，只是形式字段，不校验）→ 勾协议 → Register → 得到 32 位 hex
- **用途**：调用 `api.steampowered.com` 的官方 Web API，如 `IEconService/GetTradeOffers/v1` 列交易报价、读库存。**BUFF 绑定 Steam 账号时也要求填它**（BUFF 靠它同步你的库存/报价）
- **寿命**：长期有效，直到你主动在同一页面 Revoke/重新生成
- **⚠️ 安全红线**：这是「API Key 诈骗」的核心目标。泄露后骗子能取消真实报价、伪造一个一模一样的报价把饰品转到自己号上。**绝不要给任何自称客服/中间商的人，也不要填进陌生网站**；怀疑泄露立刻回该页面撤销
- 当前状态：**未获取**（`data/config.json` 里没有该字段）

### 2. Steam 侧的「长期 key」= **令牌种子 `identity_secret` + `shared_secret`（mafile）**

- **名字**：Steam Guard 手机令牌种子；Steamauto 配置里叫 `shared_secret` / `identity_secret`，整体存在 `mafile`（`steam_account_info.json`）
- **在哪拿**：**Watt Toolkit**（Windows，最简单）提取 Steam 令牌参数；或 Steam Desktop Authenticator（SDA）
- **用途**：
  - `identity_secret` → 对 `tag + 时间戳` 做 HMAC-SHA256 生成 `k`，用于 `mobileconf/getlist` + `/ajaxop` **自动确认交易**。这是整条链路里**唯一无法用会话 cookie 替代**的东西，也是「全自动」与「半自动」的分界线
  - `shared_secret` → 生成 Steam Guard 2FA 登录码，供账号密码登录用
- **寿命**：**永久有效，唯一不可轻易再生的凭据**（手机丢失/换机靠它恢复令牌）。备份优先级最高
- **⚠️ 风险**：SDA 提取需要先「迁移」令牌，操作不当可能丢失令牌并触发 15 天交易冷却。这是 B 档全凭证方案最大的非技术成本
- 当前状态：**未获取**（`data/config.json` 里没有相关字段）

## 二、另外两个容易混进来的

| 凭据 | 值形态 | 寿命 | 用途 | 当前状态 |
|---|---|---|---|---|
| **BUFF `session` cookie**（未来下单还要 `csrf_token`） | 约 56 字符 | 几天~几周，过期需重抓 | BUFF 查挂单簿 / 自动下单 | ✅ 已有 `data/buff_cookie.txt`（session）。`csrf_token` 下单时从 cookie 取，暂未用 |
| **Steam 会话 cookie** `steamLoginSecure` | JWT（`steamid%7C%7CeyJ...`） | **24.3 小时** | Steam pricehistory 成交价7 / priceoverview | ⚠️ `data/steam_cookie.txt` 只有第 1 行，且**已于 2026-10-30 08:46 过期** |

### Steam 会话的「长效刷新令牌」（另一种长期 key，别和 mafile 搞混）

- 旧机制：`steamRefresh_steam`，挂在 `login.steampowered.com` 域，约 200 天寿命
- 新机制（你的账号是这种）：**没有独立的 `steamRefresh_steam`**，改为 `.steampowered.com` / `store.steampowered.com` 域下一个 **2027 年才过期的长命 `steamLoginSecure`**（`25FAAC1E...` 开头的不透明令牌，非 JWT）
- 脚本已支持双令牌：`data/steam_cookie.txt` **第 1 行 = 短效 community 令牌，第 2 行 = 长命刷新令牌**。当前缺第 2 行，所以日志每隔一阵就提示「无刷新令牌，无法自动续期」，成交价7 会停用
- 补法：F12 → Application → Cookies → `https://store.steampowered.com` → 复制 `steamLoginSecure` 完整 Value，粘贴为第 2 行

## 三、全自动化分档（对应 `docs/research/Steam侧发货自动化调研_2026-10-26.md`）

| 档位 | 需要什么 | 能做到 | 手机确认 |
|---|---|---|---|
| **A 档（会话）** | 已有 `steamLoginSecure` 会话（+ 长效刷新令牌保持它不过期） | 自动接报价 / 自动发报价 | **仍需人工点**（2 秒） |
| **B 档（全凭证）** | A + `api_key` + 账号密码 + `shared_secret` + `identity_secret`（mafile） | 接/发报价 + **自动确认**，100% 无人工 | 不需要 |

按你之前定的渐进路线与红信风险偏好：先走 A 档（零新凭证、不触发新设备风控），Phase B 等 A 跑稳且愿意提取 mafile 再做，且只在专用小号上启用。

## 四、待办清单（按依赖顺序）

1. **补 Steam 长效刷新令牌**（第 2 行）→ 立刻解决成交价7 停用，零风险
2. **注册 Steam Web API Key** → 存 `data/config.json` 的 `steam_api_key`（不入库），A 档列报价要用
3. **抓一次 UUYP 发货请求**（F12 看 App 点发货的真实 API）+ 确认买家交易链接字段 → Phase A 实现的前提
4. **（可选，Phase B）Watt Toolkit 提取 mafile** → 令牌种子存密码管理器 + `data/`（不入库），只在小号启用
5. BUFF 自动下单：现有 `session` 已够查询，正式下单再补 `csrf_token` 与风控字段

> 所有凭据一律放 `data/`（已被 .gitignore 覆盖），**绝不进 git / 云盘 / 聊天窗口**。
