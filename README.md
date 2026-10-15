# VCP 自动扫描机器人（USDT 永续合约）

根据 **马克·米纳维尼（Mark Minervini）的波动收缩形态（VCP）**，自动扫描所有 USDT 本位永续合约：
**日线判断阶段（Stage 2）+ 4 小时图找 Base / 收缩 / 进场**，把符合的币画成图，并通过 **Telegram** 推送和
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

### 1. Stage 阶段（日线）
| 条件 | 默认 |
|---|---|
| 收盘 > MA50 > MA150 > MA200，且 MA200 比 20 天前高 | Minervini 趋势模板 |
| 现价 ≥ 52 周低点 × 1.30，且 ≥ 52 周高点 × 0.75 | |
| 上市不足 200 根日线 → **NEW**（用 MA50，不足 60 天用 MA20，要求均线向上） | 例：`NEW/S2` |
| 不满足时标记 S1 / S3 / S4，不提醒 | |

### 2. Base 与收缩（4h）
- 从最近的大低点（起涨点）开始，价格从高点回撤 **≥ 8%** 就开始一个 Base；**收盘重新突破 Base 高点** 时这个 Base 结束，下一个回撤 ≥ 8% 开始 B2、B3…
- 每个 Base 内用 ZigZag（3%）找摆动点；反弹收回上段跌幅 ≥ 35% 才算新的一段收缩 → C1、C2…
- Base 合格（OK / CONFIRMED）：2~6 段收缩、逐段变小（容忍 10%）、**C1 ≤ 35%**、最后一段 ≤ 15%
- 不合格标记：WIDE（C1 太深）、FAIL（没有逐段变小）、FORMING（收缩还不够 2 段）

### 3. 进场 / 止损 / 止盈
| 项目 | 规则 |
|---|---|
| Pivot | 当前 Base 最后一段收缩的高点 |
| Entry 入场 | Pivot × (1 + 0.5% 挂单缓冲) |
| Stop 止损 | 最后一段收缩低点 × (1 − 0.5%) |
| R | Entry − Stop；Risk 入场风险 = R ÷ Entry |
| BE 保本 | Entry + 0.8R：涨到这里把止损移到入场价 |
| Plan TP 计划止盈 | Entry + 5R（Plan R:R = 5） |
| Chart TP 结构止盈 | 最近 180 根 4h 的最高点（上方前高阻力）；Structure R:R = (Chart TP − Entry) ÷ R |

### 4. SCV 打分（从师父的报告反推，可调整）
| 项 | 加减分 |
|---|---|
| **S 结构** | Stage2 +1.0（非 Stage2 −2.0）；4h 收盘 > MA50 > MA150 > MA200（Close确认排列）+0.5；收盘在 MA50 下 −0.5 |
| **C 收缩** | C1偏小（≤ 20%）+0.5；Base确认 +1.0；第 1/2 个 Base +0.5；第 4 个及以后 −1.0；Pivot突破 +1.0 |
| **V 成交量** | 阴足放量（阴线且 ≥ 1.2 倍均量）−2.0；突破放量（≥ 1.5 倍）+2.0；阳足放量 +1.0；阴足缩量 +0.5；末段量缩 +1.0 |
| **评级** | 总分 ≥ 4 EXCELLENT；≥ 2 GOOD；≥ 0.5 FAIR；其余 WEAK（低于 `min_rating` 不提醒） |

### 5. 仓位与流动性
- 名义仓位 = 账户 × 每笔风险% ÷ Risk%（打到止损只亏 1%）；最多 5 倍杠杆
- 入场风险 > 10% 或第 4 个及以后的 Base → 半仓（HALF_SIZE）；评级 WEAK → SKIP
- 单笔上限 Liquidity Cap = 最新 4h 成交额 × 10%；估算 24h 成交额 = 成交额 50 均线 × 6
- 仓位规则名称：`NEW_LISTING_` / `MATURE_` + `NORMAL_SIZE` / `HALF_SIZE` / `SKIP`

只使用 **已收盘** 的K线计算，信号不会因盘中波动而“重绘”。

### 历史回放（验证规则）
每次扫描都会对每个币回放最近 300 根 4h（约 50 天），**逐根只用当时看得到的数据**：当时 Base 已确认、
评级达标、价格向上碰到 Entry → 按 Entry（跳空则按开盘价）成交，然后按同一套规则管理：
跌破 Stop 止损；涨到 BE 后止损移到入场价；到 Plan TP 止盈；持有满 180 根 4h 按收盘离场。
汇总里列出触发次数、止盈 / 保本 / 止损数、平均 R；并单独推送 **最近 3 个案例**（优先成功的），
每个都附图和当时的完整信号报告。统计不含手续费、资金费率和滑点；历史成功不代表未来。

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
