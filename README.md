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
4. 手动试跑(双击,窗口停留 + 微信回执): scripts\windows\UUYP_auto.bat
   收购扫描(双击,停留):               scripts\windows\UUYP_buy_scan.bat
5. 建计划任务(每 10 分钟)——务必用 _task 版(不弹窗、不 pause):
   schtasks /create /tn "UUYP_auto" /sc minute /mo 10 ^
     /tr "C:\path\to\steam_items_trade\scripts\windows\UUYP_auto_task.bat"
```

**Windows bat 注意事项**（踩过的坑，修改脚本时务必遵守）：

- bat 文件必须是 **CRLF 换行 + 纯 ASCII 内容**。`cmd.exe` 按 GBK 代码页解析，UTF-8 无 BOM 的中文注释会被拆成乱码命令、导致 `cd` 失效、python 在错误目录启动失败并秒退
- 脚本内 `echo` 不能含未转义的 `|`（会被当管道符）
- 双击版 `UUYP_auto.bat` 带 `pause` 和「Manual run finished」微信回执；计划任务版 `UUYP_auto_task.bat` 不带（否则任务会挂住等按键）
- **计划任务版完全静默**：所有 stdout 重定向到 `data/logs/auto_console.log`，控制台零输出——防止 Windows 控制台「快速编辑模式」选中文字时冻结进程（曾导致单轮挂死 20 小时、后续扫描全部停摆）。计划任务本身也应设为 Hidden + 15 分钟执行时限 + 忽略新实例
- 日志在 `data/logs/`，**按周命名**（`auto_log_<本周一>.txt`，如 `auto_log_2026-11-09.txt`），不是按天——找当天日期的文件会找不到；计划任务的控制台输出单独在 `auto_console.log`
- 例行调度默认**不推微信**（`push_dispatch` 未开，避免刷屏）；只有事件型消息才响：待发货订单、收购命中、转 Steam 提醒、出口评估、cookie 失效。双击版额外发一条运行回执便于确认脚本确实跑了
- 健康自检：`data/logs/auto_log_<周一>.txt` 应每 10 分钟出现一组「轮次开始 … 单轮模式结束」。若只有「轮次开始」没有结束行，说明该轮卡住；用任务计划程序看任务状态或用 `Get-ScheduledTaskInfo` 检查 `LastTaskResult`

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
| `exit_check_enable` | true | 出口评估开关(三场地×挂/砸比较推送) |
| `buff_sell_fee` | 0.015 | BUFF 卖家费率 |
| `exit_edge_min` | 0.2 | 砸单比挂单优多少元才提醒 |
| `exit_notify_hours` | 12 | 同一物品出口提醒冷却 |
| `buff_dump_gate` | 1.02 | BUFF卖一≤UUYP卖一×此值才查求购簿 |
| `spread_warn` | 0.88 | 买一/卖一低于此判定卖一虚高(UUYP/BUFF) |
| `decay_window_days` | 180 | 长期跌幅窗口(天,可设 90) |
| `hold_weeks` | 1 | 预期持有周数(跌幅按此折算) |
| `decay_cap_pct` | 5.0 | 周跌幅上限%(防极端值) |
| `spike_pct` | 0.15 | 短期急涨判定阈值 |
| `spike_base_days` | 30 | 尖峰近基线窗口(8~30日前) |
| `signal_filter_enable` / `signal_filter_max` | true / 0.75 | 修正比值超限是否剔除 |
| `spike_guard` | reprice | reprice / filter(尖峰全剔) / warn(只标) |

## 核心口径

- **比值(买)** = 场地卖一 ÷ (Steam卖一 × 0.8696)，UUYP/BUFF 谁便宜算谁；≤0.70 才入候选
- **求购比** = Steam买一 ÷ 卖一；<0.85 的市场成交以卖家砸单为主，挂卖单极慢，直接过滤
- **比值(成交)** = 场地卖一 ÷ (Steam近7日成交量加权均价 × 0.8696)；成交口径参考
- **比值(修正)**（最终决策口径，主排序）：
  ```
  退出基准 = min(成交价7 或 Steam卖一, 尖峰回落基准)
            × (1 − 周跌幅% × 预期持有周数)
  比值(修正) = 成本 ÷ (退出基准 × 0.8696)
  ```
  - **周跌幅**：180 天（可配 90）Steam 卖价曲线，按 7 日窗口取周中位数，用 **Theil-Sen**（所有点对斜率的中位数）估计——抗单次尖峰误判；正 = 每周下跌，上限 5%/周
  - **尖峰**：近 7 日中位价相对 8~90 日前基线涨幅 ≥15% → 判定短期急涨，退出估值改用**回落基准**（尖峰前中枢 ×1.05），不再用尖峰价
  - **过滤**：修正比值 >0.75 的候选不推送（防"比值虚高最后亏损"）；`spike_guard` 可设 `filter`（尖峰全剔）/ `warn`（只标不过滤），默认 `reprice`
  - 数据零额外成本：180 天曲线与形态分类共用一次 csqaq chart 调用
- **卖侧改价参考** = UUYP 实时挂单簿的**他人最低价**（UserId 排除自己；csqaq 快照有滞后幽灵价，禁止用于触发动作）
- **策略底线**：资产不允许可交易空窗——灰色带(0.70~0.75)不下架只提醒，催出而非观望
- **出口评估**（三场地 × 挂卖一/丢买一，每轮推送「最优出口提醒」）：
  - 到手 = 价 × 费率（UUYP 0.99 / BUFF 0.985 / Steam 0.8696），场地按到手价比较
  - **挂卖一 vs 丢买一** 由成交与买一的贴近度判定：Steam 用 成交价7（唯一真实成交数据）；
    UUYP/BUFF 无成交数据，用 买一/卖一 比价代理（<0.88 = 卖一虚高）
  - 卖一虚高 → 贴成交价挂（Steam 锚点=成交价7）或直接丢买一——防"僵尸卖一慢慢阴跌，
    几天后还不如当初砸买一"
  - **双挂等净**：BUFF挂价 = UUYP挂价 × 1.0051，两边到手一分不差、买家曝光翻倍
  - BUFF 求购价按需深查（gate+缓存+限量）；Steam 无求购数据（暂缺）

## 数据源与限速备忘

| 数据 | 来源 | 限制 |
|---|---|---|
| UUYP 全部接口 | api.youpin898.com | **分页 pageSize 上限 20**（传大会静默返回空）；实时挂单簿限频时静默返回空（已加缓存/退避） |
| Steam pricehistory | steamcommunity.com | 需 cookie，429 退避，每轮限量 |
| Steam priceoverview | steamcommunity.com | 无需登录，30 分钟缓存 |
| BUFF sell_order | api.buff.163.com | 需 session cookie，2s 间隔 |
| csqaq | api.csqaq.com | IP 绑定制（30s 限速），只用于买侧发现层 |

**全局熔断（防止单轮被拖死）**：Steam pricehistory 撞 429 后只退避一次，仍限则 5 分钟内本轮剩余商品全部跳过；BUFF 域名不可达时同样熔断 5 分钟（否则每件 20s 超时 ×7 件 + 每件 429 退避 120s，会撞上计划任务 15 分钟执行时限，日志停在中间、后续轮次全部被 IgnoreNew 挡掉）。熔断期回退 csqaq 快照 / 成交价留空，不阻断主流程。

## 从旧目录(skin-index)迁移

- 新仓库 `data/` 已迁移真实 config/cookie/state，可直接运行
- 历史日志与 CSV 报告未迁移（在旧目录 `logs/` `reports/`，需要时手动搬）
- **切换时注意**：旧 skin-index 的计划任务必须先停用，否则两套系统会互相打架（双重推送/双重改价）

## Roadmap（未定稿，按优先级）

1. **Phase A**：Steam 侧自动化（接报价/发报价/手机确认自动化/发货前自动撤 Steam 卖单）——蓝图见 `docs/research/Steam侧发货自动化调研` 与 `docs/reference/steampy`
2. **出口评估自动化**：目前三场地比较是推送+人工执行；后续接 BUFF 挂单/砸单 API（cookie 已具备），Steam 端依赖 Phase A
3. **Steam cookie 双令牌自动续期**（steamRefresh 或 store 域长令牌，探索中）
4. **个人成交数据回归**：记录每次买入的实际脱手天数，回校 min_turnover / min_bid_ask / spread_warn / hold_weeks
5. **BUFF 自动购买**（需 Steam 确认联动，依赖 Phase A/B）
6. `tests/` 单测补齐：口径计算、形态分类、流动性过滤、出口评估锚点、趋势信号(Theil-Sen/尖峰)
