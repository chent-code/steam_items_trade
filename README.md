# steam_items_trade

CS2 饰品「倒余额」半自动交易系统：**UUYP/BUFF 低价买入 → Steam 高价卖出**，围绕这一核心做两条自动化链路：

- **卖侧调度**（每 10 分钟）：基于 UUYP 实时挂单簿的插队改价/自动跟涨/自动上下架、待发货订单检测与微信推送
- **买侧扫描**：csqaq 跨平台挂刀榜发现候选（比值 ≤0.70）→ UUYP/BUFF 实时挂单簿验证 → Steam 成交价7 现实口径排序 → 微信推送半自动买入

> **私有仓库**。`data/`（含全部 token/cookie/交易状态）已被 gitignore，永远不会入库；克隆到新机器后需按下方说明自行填充。

## 目录结构

```
steam_items_trade/
├── core/                  # 公共 Python 模块(跨平台,所有业务逻辑)
│   ├── paths.py           #   运行时路径唯一来源(data/config/logs/reports)
│   ├── uu_auth.py         #   UUYP 客户端:登录/风控指纹/API 封装
│   ├── watcher_inv.py     #   10 分钟调度:卖侧决策 + 待发货 + 收购扫描推送入口
│   └── auto_buy_test.py   #   收购扫描:发现/验证/成交价/独立运行入口
├── scripts/
│   ├── windows/           # Windows 专用(bat,供计划任务调用)
│   └── linux/             # Linux 专用(sh + crontab 示例 + 部署脚本)
├── config/
│   └── config.example.json  # 配置模板(真实配置在 data/config.json,不入库)
├── data/                  # 运行时数据(gitignore):config.json / *cookie*.txt / *state*.json / logs/ / reports/
├── docs/
│   ├── reference/         # 开源参考代码(Steamauto / steampy / uuyoupinapi,Phase A/B 蓝图)
│   └── research/          # 策略评估与调研文档(项目决策依据,按时间归档)
├── tools/
│   ├── research/          # 第一阶段(DB 回测研究)脚本,保留备查
│   └── probes/            # API 探针小脚本
└── tests/                 # 预留(后续补单测:口径计算/形态分类等纯函数优先)
```

**后续开发约定**（项目未定稿，新功能按此归位）：

- 新的业务逻辑模块 → `core/`，路径一律 `from paths import`，禁止 `__file__` 拼相对路径
- 平台相关的启动/部署 → `scripts/windows/` 或 `scripts/linux/`（两边同名对应）
- 纯函数（口径计算、形态分类、过滤规则）优先补进 `tests/`
- 新的数据文件若含密钥 → 一律放 `data/`（已被 gitignore 覆盖）

## 快速开始

### Windows

```
1. 安装 Python 3.10+ 并 pip install -r requirements.txt
2. copy config\config.example.json data\config.json  → 填入真实 token/uid
3. cookie 放 data\steam_cookie.txt 与 data\buff_cookie.txt
4. 手动试跑: scripts\windows\UUYP_buy_scan.bat
5. 建计划任务(每 10 分钟):
   schtasks /create /tn "UUYP_auto" /sc minute /mo 10 ^
     /tr "C:\path\to\steam_items_trade\scripts\windows\UUYP_auto.bat"
```

### Linux

```
1. ./scripts/linux/setup_linux.sh        # 建 venv + 依赖 + data/ 骨架
2. 编辑 data/config.json,放入两个 cookie 文件
3. 手动试跑: ./scripts/linux/uuyp_buy_scan.sh
4. crontab -e,参照 scripts/linux/crontab.example 配置每 10 分钟调度
```

## 配置项速查

| 键 | 默认 | 说明 |
|---|---|---|
| `uu_token` / `uu_device_token` | 必填 | UUYP 登录凭证 |
| `csqaq_token` | 必填 | csqaq 数据服务(买侧发现层) |
| `wxpusher.app_token` / `uid` | 必填 | 微信推送 |
| `ratio_max` | 0.70 | 买入过滤:场地卖一 ÷ (Steam卖一×0.8696) |
| `min_turnover` | 10 | 日成交量下限(买家到货率硬上限) |
| `min_bid_ask` | 0.85 | Steam求购比下限(买一÷卖一,滤掉砸单为主的市场) |
| `accept_ratio` | 0.75 | 卖侧:比值 ≥0.75 自动上架 |
| `delist_ratio` | 0.70 | 卖侧:已上架跌破 0.70 下架转 Steam |
| `undercut_pct` | 1.0 | 插队幅度%(对他人最低价) |
| `buy_max_per_item` | 3 | 单品种最多买入件数(挂单簿达标档位数) |
| `buy_scan_enable` / `buy_scan_pages` | true / 8 | 调度内收购扫描开关与页数 |

## 核心口径

- **比值(买)** = 场地卖一 ÷ (Steam卖一 × 0.8696)，UUYP/BUFF 谁便宜算谁；≤0.70 才入候选
- **求购比** = Steam买一 ÷ 卖一；<0.85 的市场成交以卖家砸单为主，挂卖单极慢，直接过滤
- **比值(成交)** = 场地卖一 ÷ (Steam近7日成交量加权均价 × 0.8696)；推送主排序，★=<0.70 硬机会，⚠=>0.75 谨慎
- **卖侧改价参考** = UUYP 实时挂单簿的**他人最低价**（UserId 排除自己；csqaq 快照有滞后幽灵价，禁止用于触发动作）
- **策略底线**：资产不允许可交易空窗——灰色带(0.70~0.75)不下架只提醒，催出而非观望

## 数据源与限速备忘

| 数据 | 来源 | 限制 |
|---|---|---|
| UUYP 全部接口 | api.youpin898.com | **分页 pageSize 上限 20**（传大会静默返回空）；实时挂单簿限频时静默返回空（已加缓存/退避） |
| Steam pricehistory | steamcommunity.com | 需 cookie，429 退避，每轮限量 |
| Steam priceoverview | steamcommunity.com | 无需登录，30 分钟缓存 |
| BUFF sell_order | api.buff.163.com | 需 session cookie，2s 间隔 |
| csqaq | api.csqaq.com | IP 绑定制（30s 限速），只用于买侧发现层 |

## 从旧目录(skin-index)迁移

- 新仓库 `data/` 已迁移真实 config/cookie/state，可直接运行
- 历史日志与 CSV 报告未迁移（在旧目录 `logs/` `reports/`，需要时手动搬）
- **切换时注意**：旧 skin-index 的计划任务必须先停用，否则两套系统会互相打架（双重推送/双重改价）

## Roadmap（未定稿，按优先级）

1. **Phase A**：Steam 侧自动化（接报价/发报价/手机确认自动化/发货前自动撤 Steam 卖单）——蓝图见 `docs/research/Steam侧发货自动化调研` 与 `docs/reference/steampy`
2. **Steam cookie 双令牌自动续期**（steamRefresh 或 store 域长令牌，探索中）
3. **阴跌防护 v2**：pricehistory 7 日斜率 × 预期持有期 vs 毛利(成交) 的量化过滤
4. **个人成交数据回归**：记录每次买入的实际脱手天数，回校 min_turnover / min_bid_ask
5. **BUFF 自动购买**（需 Steam 确认联动，依赖 Phase A/B）
6. `tests/` 单测补齐：口径计算、形态分类、流动性过滤
