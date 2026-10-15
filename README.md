# VCP 自动扫描机器人（USDT 永续合约）

根据 **马克·米纳维尼（Mark Minervini）的波动收缩形态（VCP）**，自动扫描所有 USDT 本位永续合约：
**日线判断阶段（先翻倍 + 站上 MA150 = Stage 2）+ 4 小时图找 Base / 收缩 / 进场**，把符合的币画成图，并通过 **Telegram** 推送和
“师父格式”一样的完整信号报告（Stage、Base 编号与历史、C 明细、SCV 打分、Entry/Stop/BE/TP、R:R、仓位、成交量、流动性）。

- 数据免费：Binance / Bybit / OKX / Gate 公开行情接口，**不需要注册、不需要 API Key**，连不上会自动换下一家
- 规则全部客观量化（见下方「判定规则」），没有主观判断
- 两种信号：
  - 👀 **SETUP**：当前 Base 已确认，价格在 Entry 下方 3% 内 → 挂突破单
  - 🚀 **BREAKOUT**：4h 收盘站上 Entry → 突破
- 同一个信号只提醒一次；每次扫描还会用同一套规则 **回放历史**，把过去真实触发的案例连图带报告发给你

![](docs/demo_signal.png)

方框 = Base（B1、B2…）；黄色虚线框 = 每段收缩（B2C1 16.8%）；右侧标签 = Entry / Stop / BE / Plan TP / Chart TP；
左上角 = 信息面板；下方 = 4h 成交额与 50 均线。（模拟数据演示图）

---

## 小白一步一步来（Windows，约 15 分钟）

### 第 1 步：安装 Python
1. 打开 https://www.python.org/downloads/ ，点黄色按钮下载
2. 运行安装包，**一定要勾选最下面的 `Add python.exe to PATH`**，再点 `Install Now`

### 第 2 步：下载本程序
1. 在本 GitHub 页面点绿色 **Code** 按钮 → **Download ZIP**
2. 解压到一个好找的位置，例如 `D:\vcp`
3. 双击 **`1_install.bat`**，等它显示 `Done!`

> 现在就可以双击 **`2_scan_once.bat`** 试扫一次了。结果和图表保存在 `output` 文件夹。
> 如果显示“所有交易所都连接失败”，说明你的网络连不上交易所，需要开启代理/VPN（和你平时打开交易所网页一样）。

### 第 3 步：设置 Telegram 提醒（免费）
1. 在 Telegram 搜索 **@BotFather** → 发送 `/newbot` → 按提示起个名字
2. BotFather 会给你一串 **token**（像 `7123456789:AAHxxxx...`）
3. 用记事本打开 `config.yaml`，把 token 填进去：
   ```yaml
   telegram:
     bot_token: "7123456789:AAHxxxx..."
     chat_id: ""
   ```
4. 在 Telegram 里找到你刚建的机器人，点 **Start**，随便发一句 `hi`
5. 双击 **`5_telegram_test.bat`**，屏幕会显示 `chat_id = 123456789`
   （或者在 Telegram 搜索 **@userinfobot**，点 Start，它回复的 `Id` 就是你的 chat_id。
   ⚠️ chat_id **不是** token 冒号前面那串数字，那是机器人自己的 ID）
6. 把这个数字填进 `config.yaml` 的 `chat_id`，保存
7. 再双击一次 **`5_telegram_test.bat`**，Telegram 收到“提醒测试成功” ✅

### 第 4 步：让它一直自动扫描
双击 **`3_run_forever.bat`**，窗口保持打开即可，默认每 60 分钟扫一次，符合 VCP 就推送带图的提醒到 Telegram。

> 想单独看某个币为什么符合/不符合？双击 `4_check_one_symbol.bat`，输入如 `CTRUSDT`，会输出完整信号报告、没有触发的原因、历史回放结果，并画图。

Mac / Linux：在终端运行 `./run_mac_linux.sh`

---

## 不想开着电脑？用 GitHub 免费云端自动跑（可选）

本仓库已包含 `.github/workflows/vcp-scan.yml`，每 2 小时自动扫描一次（GitHub 定时任务常会延迟或跳过，所以排得比较密；汇总消息最多 4 小时发一次）。
1. 仓库页面 → **Settings → Secrets and variables → Actions → New repository secret**
2. 添加 `TELEGRAM_BOT_TOKEN` 和 `TELEGRAM_CHAT_ID` 两个 secret（值同第 3 步；chat_id 可用 @userinfobot 查到）。**不要把 token 直接写进仓库里的 `config.yaml`**，别人看到就能用你的机器人
3. 打开 **Actions** 页面 → 选 **VCP Scan** → **Run workflow** 手动试一次，Telegram 会收到一条「📊 VCP 扫描完成」汇总
4. **定时自动运行只在默认分支（main）上生效**：代码必须合并到 `main`，GitHub 才会每 4 小时自动跑

注意：GitHub 的服务器在美国，Binance / Bybit 会拒绝美国 IP，程序会自动改用 OKX 或 Gate 的数据。
同一个币在各交易所的走势几乎一样，形态判断结果基本一致；你仍可在自己常用的交易所下单。
（私有仓库每月有 2000 分钟免费额度，按默认频率大约用 500 分钟左右。）

---

## 判定规则（全部可在 `config.yaml` 的 `strategy:` 段调整）

### 1. Stage 2（日线，Minervini 趋势模板 8 条）
| # | 条件 |
|---|---|
| 1 | 价格 > MA150 且 > MA200 |
| 2 | MA150 > MA200 |
| 3 | MA200 向上至少 1 个月（Minervini 更偏好 4~5 个月） |
| 4 | MA50 > MA150 且 > MA200 |
| 5 | 价格 > MA50 |
| 6 | 价格 ≥ 52 周低点 × 1.30 |
| 7 | 价格 ≥ 52 周高点 × 0.75 |
| 8 | **RS 相对强度 ≥ 70**：每次扫描把所有币按“近 3 个月 40% + 前三个季度各 20%”的涨幅排名（1~99） |

- 上市不足 200 天的币（标记 **NEW**）算不出 MA200，只用算得出来的均线（MA150 / MA50 / MA20）
- **前期涨幅 ≥ 100%（翻倍）** 是加分项；设 `require_advance: true` 可变成硬性条件（比 Minervini 更严）

### 2. Base 与收缩（4h）
- 从那段大涨的 **起涨低点** 开始数 Base：价格从高点回撤 ≥ 8% 开始一个 Base，**收盘重新突破 Base 高点** 时结束
- **超过 2 个 Base 判定失效**（标记 LATE，不提醒）
- 每个 Base 内：**C1 = Base 最高点 → 回调最低点**；之后每段收缩的 **低点必须比上一段高**，
  没抬高的（跌破了前面的低点）说明还是同一次回调，自动并入前一段，所以不会出现一堆 C
- Base 合格（OK / CONFIRMED）：2~6 段收缩、逐段变小（容忍 10%）、**C1 ≤ 35%**、最后一段 ≤ 10%
- 不合格标记：WIDE（C1 太深）、FAIL（没有逐段变小）、FORMING（收缩还不够 2 段）、LATE（第 3 个及以后的 Base）

### 3. 进场 / 止损 / 出场
| 项目 | 规则 |
|---|---|
| Pivot | 当前 Base 最后一段收缩的高点 |
| Entry 入场 | Pivot × (1 + 0.5% 挂单缓冲) |
| Stop 止损 | 最后一段收缩低点 × (1 − 0.5%)；**入场风险 > 10% 不做**（Minervini：单笔止损不超过 10%） |
| R | Entry − Stop |
| Chart TP 结构止盈 | 最近 180 根 4h 的最高点（上方前高阻力） |

出场方案（`exit_profile`，默认 `minervini`）。**历史回放会对同一批入场把三种都算一遍**，汇总里直接对比哪种在加密货币上表现更好：

| 方案 | 保本 | 减仓 | 剩余仓位 |
|---|---|---|---|
| `minervini` | +2R 止损移到入场价 | +3R 卖 50%（逢强卖出） | 4h 收盘跌破 MA120（≈ 日线 20MA）离场 |
| `master`（师父报告的参数） | +0.8R | — | +5R 全部止盈 |
| `qullamaggie`（公开资料整理，非官方） | 减仓时 | 持有 3 天后若盈利卖 50% | 4h 收盘跌破 MA60（≈ 日线 10MA）离场 |

### 4. SCV 扣分制（师父的做法：走满分，慢慢扣分，只做高分）
VCP 事后看到处都是（“万物皆可 VCP”），拿历史成功案例来看只会自我安慰。真正有用的是 **在当下给形态打分，低分的不做**。

**Score 满分 3 分**：S（结构）、C（收缩）、V（成交量）三项各自加减分，最后每项 **正数记 +1、被扣成负数记 −1**，三项相加。
例：师父的 CTRUSDT 是 S +1.5、C +3.0、V −2.0（阴足放量）→ Score = 1 + 1 − 1 = **1（GOOD）**。

| 项 | 加分 | 扣分 |
|---|---|---|
| **S 结构** | Stage2 +1.0；4h Close确认排列 +0.5；前期翻倍 +0.5；RS ≥ 85 +0.5 | **不在 Stage 2 −2.0**；收盘在 MA50 下 −0.5；RS < 70 −1.0 |
| **C 收缩** | C1偏小（≤ 20%）+0.5；Base确认 +1.0；第 1/2 个 Base +0.5；Pivot突破 +1.0 | Base 未确认 −1.0；超过 2 个 Base −2.0；**最近 3 根 4h 有长上影线（上影线 ≥ K线长度 50%）−2.0** |
| **V 成交量** | 突破放量（≥ 1.5 倍 MA50）+2.0；突破量超过 MA50 +1.0；末段量缩 +1.0；阳足放量 / 阴足缩量 +0.5 | **无量突破（突破K线成交额没超过 MA50）−2.0**；阴足放量 −2.0；**最近 24h 成交额 < 100 万 USDT（流动性差）−2.0** |

| Score | 评级 | 默认 |
|---|---|---|
| 3 | EXCELLENT | 提醒 |
| 2 | STRONG | 提醒 |
| 1 | GOOD | 提醒（`min_rating: GOOD`） |
| 0 | FAIR | 不提醒 |
| 负数 | WEAK | 不提醒 |

**怎么检验扣分制有没有用**：历史回放会把所有 VCP 突破都模拟一遍（不管分数高低），然后按 Score 分组列出胜率和平均 R，
并对比“高分照做”和“低分不做”两组。如果高分组没有明显比低分组好，就说明扣分规则要调整。

### 5. 仓位与流动性
- 名义仓位 = 账户 × 每笔风险% ÷ Risk%（打到止损只亏 1%）；最多 5 倍杠杆
- 入场风险 > 10% 或评级低于 `min_rating` → SKIP（不做）
- 单笔上限 Liquidity Cap = 最新 4h 成交额 × 10%；估算 24h 成交额 = 成交额 50 均线 × 6
- 仓位规则名称：`NEW_LISTING_` / `MATURE_` + `NORMAL_SIZE` / `HALF_SIZE` / `SKIP`

只使用 **已收盘** 的K线计算，信号不会因盘中波动而“重绘”。

### 历史回放（验证规则）
每次扫描都会对每个币回放最近 300 根 4h（约 50 天），**逐根只用当时看得到的数据**：当时 Base 已确认、
评级达标、价格向上碰到 Entry → 按 Entry（跳空则按开盘价）成交，然后按同一套规则管理：
跌破 Stop 止损；涨到 BE 后止损移到入场价；到 Plan TP 止盈；持有满 180 根 4h 按收盘离场。
汇总里列出触发次数、止盈 / 保本 / 止损数、平均 R；并单独推送 **最近 3 个案例**（优先成功的），
每个都附图和当时的完整信号报告。统计不含手续费、资金费率和滑点；历史成功不代表未来。

### 规则来源与“后来者”
- 趋势模板、VCP（收缩逐段变小、末段量缩、Pivot 突破）、10% 最大止损、保本与逢强卖出：来自 Mark Minervini 的
  *Trade Like a Stock Market Wizard* / *Think & Trade Like a Champion*（本程序参数取自公开的整理资料，个别数字各来源略有出入，例如 52 周低点有写 25% 也有写 30%，这里用书中的 30%）
- 后来者：Oliver Kell（2020 美国投资冠军赛 +941%）、J Law（2024 年 +353.9%，破了 Minervini 2021 年 +334.8% 的纪录）、
  Kristjan Kullamägi（Qullamaggie，收益为自述，未经比赛核实）。他们都是在 Minervini/O'Neil 的基础上加了更短线的进出场，
  **但冠军赛是单年成绩、组别不同，不能证明谁的方法长期更好**。所以这里不直接替换，而是把更短线的出场方式做成可选方案，用历史回放在加密货币数据上实际比较

### 常用调整
- 想要更多信号：`min_rating: WEAK`、`near_entry_pct: 0.05`、`max_last_depth: 0.20`
- 想要更严格：`min_rating: GOOD`、`min_contractions: 3`
- 只扫大币：`min_quote_volume_24h: 100000000`（1 亿 USDT）
- 账户资金和每笔风险：`account:` 段

## 多个账号接收通知
`config.yaml` 里 `telegram.chat_id` 可以填多个，用英文逗号分隔，例如 `"123456789,1134208027"`。
GitHub Secrets 里的 `TELEGRAM_CHAT_ID` 也会一起收到。每个账号都要先在 Telegram 里打开机器人点 **Start**。

## 输出文件
- `output/<时间>/signals.csv`：本轮 SETUP / BREAKOUT 信号
- `output/<时间>/all_results.csv`：所有币的判定明细和不符合原因
- `output/<时间>/*.png`：符合形态的币的图（含交易计划线）
- `output/<时间>/validation_trades.csv`：历史回放的每一笔模拟交易

## 文件说明
| 文件 | 作用 |
|---|---|
| `scanner.py` | 主程序 |
| `vcp/engine.py` | 多周期 VCP 引擎：Stage、Base 编号、SCV、交易计划、历史回放 |
| `vcp/detector.py` | ZigZag 与收缩拆分 |
| `vcp/report.py` | 信号报告文字（师父格式） |
| `vcp/data.py` | 各交易所免费行情接口 |
| `vcp/chart.py` | 画图（Base 方框、收缩框、价位线、信息面板） |
| `vcp/notify.py` | Telegram / Discord 提醒 |
| `config.yaml` | 所有设置 |
| `tests/` | 自动测试（`python -m pytest`） |

## 免责声明
本程序只是按客观规则识别图形并提醒，**不构成任何投资建议**。永续合约有杠杆，风险极高，请自行设置止损
（提醒里给出的“末段低点”可作为参考止损位）。
