# `sap_hana_visit_orders` 与 `visit_orders` 字段对比

更新时间：2026-04-16

## 1. 两张表的定位

### `sap_hana_visit_orders`
- 角色：SAP HANA 推送过来的原始事实表
- 粒度：**1 条记录 = 1 张到诊单 `DZDH`**
- 分诊明细：放在 `fzdata` JSON 数组里
- 唯一键：`(jgbm, dzdh)`
- 当前字段数：`35`

### `visit_orders`
- 角色：当前系统内部仍在使用的兼容业务表
- 粒度：**1 条记录 = 1 张 `DZDH` 下展开后的一条分诊明细**
- 分诊明细：已经从 `fzdata` 拆平到行级
- 唯一键：`(dzdh, dzseg)`
- 当前字段数：`55`

一句话理解：
- `sap_hana_visit_orders` 是“原始主单表”
- `visit_orders` 是“把 SAP HANA 原始数据展开、翻译、补齐后的兼容表”

---

## 2. 当前重名字段

这部分字段名在两张表里都存在，语义最接近 SAP HANA 原始字段。

| 字段 | `sap_hana_visit_orders` | `visit_orders` | 说明 |
| --- | --- | --- | --- |
| `id` | 有 | 有 | 本地主键 |
| `jgbm` | 有 | 有 | 机构编码 |
| `dzdh` | 有 | 有 | 到诊单号 |
| `yydh` | 有 | 有 | 预约单号 |
| `crtdt` | 有 | 有 | 创建日期 |
| `crttm` | 有 | 有 | 创建时间 |
| `dzsta` | 有 | 有 | 到诊状态代码 |
| `dztyp` | 有 | 有 | 到诊类型代码 |
| `fzuer` | 有 | 有 | 顾问编码 |
| `fzuer_long` | 有 | 有 | 顾问姓名 |
| `advyq` | 有 | 有 | 院前美学顾问编码 |
| `yyuer` | 有 | 有 | 预约医生编码 |
| `kunr` | 有 | 有 | 客户号 |
| `ninam` | 有 | 有 | 客户姓名 |
| `kulvl_dq` | 有 | 有 | 当前会员星级 |
| `bhkx` | 有 | 有 | 补划扣标识 |
| `remark_dz` | 有 | 有 | 到诊需求备注 |
| `jgks` | 有 | 有 | 机构科室 |

说明：
- 这批字段是最容易直接和 SAP HANA 对齐的
- 如果未来进一步瘦身 `visit_orders`，这批字段通常应优先保留

---

## 3. 只在 `sap_hana_visit_orders` 里存在的字段

这些字段是 SAP HANA 原始结构独有的，很多还没有在 `visit_orders` 中原样保留。

### 3.1 顶层原始字段

| 字段 | 含义 | 现状 |
| --- | --- | --- |
| `kusex` | 性别代码 | 在 `visit_orders` 里转成 `customer_gender` |
| `kutyp_dq` | 当前客户类型 | 在 `visit_orders` 里转成 `khlx` |
| `kut30_dq` | T30 客户类型 | 在 `visit_orders` 里转成 `khlx_t30` |
| `kusta_dq` | 当前客户类型2 | 在 `visit_orders` 里转成 `khlx2` |
| `dzly` | 到诊来源代码 | 在 `visit_orders` 里转成 `dzly_txt` |
| `dymd` | 到院目的代码 | 在 `visit_orders` 里转成 `dymd_txt` |
| `vipkf` | 客服编码 | 在 `visit_orders` 里转成 `vipkf` |
| `d_fzuer` | 当前顾问编码 | 在 `visit_orders` 里转成 `fzr_id_dq` |
| `d_vipkf` | 当前客服编码 | 在 `visit_orders` 里转成 `d_vipkf` |
| `kusrc` | 渠道来源1代码 | 在 `visit_orders` 里转成 `qdly1_txt` |
| `kusrc2` | 渠道来源2代码 | 在 `visit_orders` 里转成 `qdly2_txt` |
| `bjzx` | 标记字段 | 当前未下沉到 `visit_orders` |

### 3.2 原始结构与接收痕迹字段

| 字段 | 含义 |
| --- | --- |
| `fzdata` | SAP HANA 推送的分诊明细数组 |
| `source_payload` | 原始请求报文 |
| `last_received_at` | 最近一次接收到该单据的时间 |
| `created_at` | 原始表创建时间 |
| `updated_at` | 原始表更新时间 |

这部分字段说明：
- 它们非常适合作为“原始事实保留层”
- 但不适合作为页面、录音匹配、接诊逻辑直接读取的字段

---

## 4. `fzdata` 明细字段

SAP HANA 的分诊信息不是直接列在主表里，而是在 `fzdata` 数组中。

| `fzdata` 字段 | 含义 | 在 `visit_orders` 中对应字段 |
| --- | --- | --- |
| `FZDH` | 分诊单号 | `fzdh` |
| `ADVXC` | 现场美学顾问编码 | `advxc` |
| `ADVXC_LONG` | 现场美学顾问姓名 | `advxc_long` |
| `ASSXC` | 美学顾问助理编码 | `assxc` |
| `FZSJ` | 分诊时间 | `fzsj` |
| `FZSTA` | 分诊状态代码 | `fzsta` / `fzsta_txt` |
| `DDSC` | 等待时长 | `ddsc` |
| `JCSTA` | 成交状态代码 | `jcsta` / `jcsta_txt` |

关键点：
- `sap_hana_visit_orders` 是“1 张 `DZDH` + 多条 `fzdata`”
- `visit_orders` 是把 `fzdata` 拆开后，变成“1 条 `DZDH` 明细 = 1 行”

---

## 5. 只在 `visit_orders` 里存在的字段

这部分是你后续最需要继续筛的区域。它们大致分成 4 类：

### 5.1 `fzdata` 拆平后新增的行级字段

| 字段 | 来源 | 说明 |
| --- | --- | --- |
| `dzseg` | 本地派生 | `DZDH` 下的行项目编号 |
| `fzdh` | `FZDATA.FZDH` | 分诊单号 |
| `advxc` | `FZDATA.ADVXC` | 现场美学顾问编码 |
| `advxc_long` | `FZDATA.ADVXC_LONG` | 现场美学顾问姓名 |
| `assxc` | `FZDATA.ASSXC` | 顾问助理编码 |
| `fzsj` | `FZDATA.FZSJ` | 分诊时间 |
| `fzsta` | `FZDATA.FZSTA` | 分诊状态代码 |
| `ddsc` | `FZDATA.DDSC` | 等待时长 |
| `jcsta` | `FZDATA.JCSTA` | 成交状态代码 |

### 5.2 SAP HANA 代码值翻译后的文本字段

| 字段 | 来源 | 说明 |
| --- | --- | --- |
| `fzsta_txt` | `fzsta` 翻译 | 分诊状态文本 |
| `dzsta_txt` | `dzsta` 翻译 | 到诊状态文本 |
| `dztyp_txt` | `dztyp` 翻译 | 到诊类型文本 |
| `jcsta_txt` | `jcsta` 翻译 | 成交状态文本 |
| `dymd_txt` | `dymd` 翻译 | 到院目的文本 |
| `dzly_txt` | `dzly` 翻译 | 到诊来源文本 |

这批字段的特点：
- 不是 SAP HANA 原样字段
- 但前端展示、筛选、人工判断时很方便

### 5.2.1 代码字段与中文展示字段的保留建议

下面这组字段在真实 SAP HANA 数据里基本都是代码值，不是业务人员直接可读的中文：

| SAP HANA 字段 | 当前真实值示例 | 建议 |
| --- | --- | --- |
| `DZSTA` | `C`、`1` | 保留代码字段，同时保留中文展示能力 |
| `DZLY` | `N`、`Y` | 保留代码字段，同时保留中文展示能力 |
| `DYMD` | `B`、`A`、`X`、`D`、`Z`、`C` | 保留代码字段，同时保留中文展示能力 |
| `DZTYP` | `4`、`1`、`3`、`5`、`Z`、`2` | 保留代码字段，同时保留中文展示能力 |
| `FZSTA` | `A`、`1` | 保留代码字段，同时保留中文展示能力 |
| `JCSTA` | `Y`、`N`、`Z` | 保留代码字段，同时保留中文展示能力 |

对应到当前 `visit_orders`，这意味着下面这批中文字段仍然有价值：

- `dzsta_txt`
- `dzly_txt`
- `dymd_txt`
- `dztyp_txt`
- `fzsta_txt`
- `jcsta_txt`

这 6 个字段如果未来不想继续落库，也至少要保留接口层/前端层的码表翻译能力；否则页面、录音匹配说明、人工复核都会退化成直接看代码值。

另外还有 4 个字段虽然也是代码值，但处理方式和上面不同：

| SAP HANA 字段 | 当前真实值示例 | 建议 |
| --- | --- | --- |
| `KUSEX` | `F`、`M` | 不必再单独保留 `*_txt`，保留代码或统一转换为 `男/女` 即可 |
| `KUTYP_DQ` | `V`、`Q` | 保留代码字段；如果页面要直接展示，建议后续补码表 |
| `KUT30_DQ` | `V`、`Q` | 保留代码字段；如果页面要直接展示，建议后续补码表 |
| `KUSTA_DQ` | `V1`、`Q1`、`Q2`、`Q3` | 保留代码字段；如果页面要直接展示，建议后续补码表 |

这 4 个字段的建议是：

- `KUSEX`：可以继续保留成 `customer_gender` 这种归一化结果，不必专门再建 `kusex_txt`
- `KUTYP_DQ / KUT30_DQ / KUSTA_DQ`：当前系统最好保留原始代码字段；如果后面业务侧明确需要直接看中文，再决定是否增加中文翻译列或统一码表服务

### 5.2.2 当前已确认的中文码表

下面这批中文解释已经在代码注释和同步逻辑里保存，可作为后续字段清理和页面展示的基础码表：

| 字段 | 代码 | 中文解释 |
| --- | --- | --- |
| `KUSEX` | `M` | 男 |
| `KUSEX` | `F` | 女 |
| `DZSTA` | `1` | 未分诊 |
| `DZSTA` | `A` | 已确认 |
| `DZSTA` | `C` | 已分诊 |
| `DZSTA` | `D` | 已取消 |
| `KUTYP_DQ` | `Q` | 潜客/新客 |
| `KUTYP_DQ` | `V` | 会员/老客 |
| `KUT30_DQ` | `Q` | 潜客/新客 |
| `KUT30_DQ` | `V` | 会员/老客 |
| `KUSTA_DQ` | `Q1` | 建档未上门 |
| `KUSTA_DQ` | `Q2` | 上门未成交 |
| `KUSTA_DQ` | `Q3` | 体验会员 |
| `KUSTA_DQ` | `V1` | 付费会员 |
| `DZLY` | `Y` | 已预约 |
| `DZLY` | `N` | 未预约 |
| `DYMD` | `A` | 咨询 |
| `DYMD` | `B` | 治疗 |
| `DYMD` | `C` | 手术 |
| `DYMD` | `D` | 复查 |
| `DYMD` | `X` | 未到院购买 |
| `DYMD` | `Z` | 其他 |
| `DZTYP` | `1` | 初诊 |
| `DZTYP` | `2` | 复诊 |
| `DZTYP` | `3` | 再咨 |
| `DZTYP` | `4` | 诊疗 |
| `DZTYP` | `5` | 未到院购买 |
| `DZTYP` | `Z` | 其他 |
| `FZSTA` | `1` | 待接诊 |
| `FZSTA` | `A` | 已接诊 |
| `JCSTA` | `N` | 未成交 |
| `JCSTA` | `Y` | 已成交 |
| `JCSTA` | `Z` | 已治疗 |
| `JGKS` | `JGKS01` | 口腔科 |
| `JGKS` | `JGKS02` | 皮肤科 |
| `JGKS` | `JGKS03` | 外科 |
| `JGKS` | `JGKS04` | 微整科 |
| `JGKS` | `JGKS05` | 中医 |
| `JGKS` | `JGKS06` | 纹绣 |
| `JGKS` | `JGKS07` | 会籍 |
| `JGKS` | `JGKS08` | 毛发移植科 |
| `JGKS` | `JGKS09` | 非手术 |
| `JGKS` | `JGKS10` | 私密中心 |
| `JGKS` | `JGKS11` | 纤体中心 |
| `JGKS` | `JGKS12` | 植发中心 |
| `JGKS` | `JGKS13` | 形体私密中心 |
| `JGKS` | `JGKS14` | SPA中心 |

### 5.3 同义改名、兼容转换、补齐字段

| `visit_orders` 字段 | 对应 SAP HANA 来源 | 说明 |
| --- | --- | --- |
| `khlx` | `kutyp_dq` | 客户类型改名 |
| `khlx_t30` | `kut30_dq` | T30 客户类型改名 |
| `khlx2` | `kusta_dq` | 客户类型2改名 |
| `vipkf` | `vipkf` | 客服编码改名 |
| `fzr_id_dq` | `d_fzuer` / `fzuer` | 当前顾问编码 |
| `d_vipkf` | `d_vipkf` / `vipkf` | 当前客服编码 |
| `advyq_name` | `advyq` + 员工映射 | 院前顾问姓名 |
| `fzr_name_dq` | `d_fzuer` / `fzuer` + 员工映射 | 当前顾问姓名 |
| `customer_gender` | `kusex` | 性别归一化 |
| `customer_birthday` | 本地补数 | 当前仍为空 |
| `fzuer_long` | `fzuer_long` | 顾问姓名原样保留 |
| `qdly1_txt` | `kusrc` | 当前直接存代码/来源值 |
| `qdly2_txt` | `kusrc2` | 当前直接存代码/来源值 |

### 5.4 本地同步和时间兼容字段

| 字段 | 说明 |
| --- | --- |
| `sjrq` | 兼容的数据日期 |
| `fzrq` | 分诊日期 |
| `jdrq` | 建档日期 |

### 5.5 当前仍然偏兼容保留的字段

这部分字段虽然还在 `visit_orders` 里，但已经不属于 SAP HANA 的核心事实字段：

| 字段 | 当前状态 |
| --- | --- |
| `khlx_yg` | 兼容保留，目前无 SAP HANA 原始来源 |
| `hylx_yg` | 兼容保留，目前无 SAP HANA 原始来源 |
| `qd1jfl` | 兼容保留，目前未由 SAP HANA 真实下发 |
| `qd2jfl` | 兼容保留，目前未由 SAP HANA 真实下发 |
| `jzsj` | 兼容保留，目前未由 SAP HANA 真实下发 |
| `jzrq` | 兼容保留，目前未由 SAP HANA 真实下发 |
| `fzrid` | 本地兼容字段，当前多由 `advxc` 或当前顾问编码顶替 |

### 5.6 已在本轮清理的兼容字段

2026-04-16 这一轮已经从 `visit_orders` 中删除：

- `yybm`
- `yyjc`
- `advyq_dq`
- `advyq_dq_name`
- `mdfdt`
- `mdftm`
- `synced_at`
- `jzks`
- `z1jflt`

---

## 6. 一眼看懂的结构差异

### `sap_hana_visit_orders`
- 更像“原始接收表”
- 保留原始报文、原始代码、原始结构
- 一条 `DZDH` 只有一行
- 多个分诊明细放在 `fzdata`

### `visit_orders`
- 更像“系统兼容业务表”
- 已经把 `fzdata` 拆平
- 已经把不少 SAP HANA 字段改名
- 已经把代码值翻译成中文文本
- 已经补了员工姓名、机构简称、同步时间等本地字段

---

## 7. 现在最适合继续清理的字段范围

如果后续继续坚持“完全以 SAP HANA 为准”，那么 `visit_orders` 里最值得继续审的，是下面这组仍偏兼容保留的字段：

- `khlx_yg`
- `hylx_yg`
- `qd1jfl`
- `qd2jfl`
- `jzsj`
- `jzrq`
- `fzrid`
- `customer_birthday`

这些字段里有些已经没有稳定上游来源，有些只是为了兼容旧页面暂时保留。

---

## 8. 建议的保留原则

### 应优先保留
- SAP HANA 原始就有、且主链路在用的字段
- `fzdata` 拆平后用于录音匹配、接诊详情、人工核对的字段
- 代码翻译后的文本字段

### 可以继续评估
- 只是兼容旧命名、但和 SAP HANA 事实字段一一对应的字段
- 机构简称、姓名映射、同步时间等本地辅助字段

### 可以进入下一轮清理候选
- 没有稳定 SAP HANA 来源
- 只是为了旧界面兼容保留
- 现在已经没有真实业务依赖的字段

---

## 9. 只看 SAP HANA 与回传场景时，`visit_orders` 应保留哪些字段

这一节采用更严格的判断标准：

- 只考虑 `sap_hana_visit_orders` 里真实存在的字段
- 只考虑“咨询单回传”和“未来标签回传”要定位到哪位客户、哪次到诊、哪条分诊明细
- **不再考虑和旧 `cur.visit_order` 兼容**

### 9.1 当前咨询单回传已确认会用到的字段

按当前代码 [sap_consultation.py](/opt/badge/apps/api/src/smart_badge_api/sap_consultation.py) 来看，咨询单回传实际依赖的是：

| 用途 | 字段 |
| --- | --- |
| 机构定位 | `jgbm` |
| 客户定位 | `kunr` |
| 到诊 / 分诊定位 | `dzdh`、`fzdh` |
| 接诊人定位 | `advxc` / `fzuer` |
| 面诊医生 | `yyuer` |
| 咨询时间回填 | `crtdt`、`crttm`、`fzsj` |
| 预览与日志展示 | `ninam`、`advxc_long` |

所以如果只从“当前咨询单回传”看，`visit_orders` 至少要能稳定提供上面这一组信息。

### 9.2 未来标签回传最可能需要的字段

当前代码库里**没有查到独立的“标签回传接口实现”**，因此这里是基于现有业务链路的推断。

如果未来要把标签回传给 SAP，最稳妥的最小定位集通常也还是这几类：

| 用途 | 建议字段 |
| --- | --- |
| 机构 | `jgbm` |
| 客户 | `kunr` |
| 本次到诊 | `dzdh` |
| 本次分诊 | `fzdh` |
| 本次接诊顾问 | `advxc` / `fzuer` |
| 基础时间 | `crtdt`、`crttm`、`fzsj` |

换句话说，标签回传通常首先要解决的是“这些标签落到哪个客户、哪次分诊、哪个机构”，而不是旧宽表那套展示兼容字段。

### 9.3 只按“回传闭环”看，建议保留的最小字段集

如果把 `visit_orders` 彻底收口成“SAP HANA 分诊明细业务表”，我建议最小保留下面这组：

| 类别 | 建议保留字段 |
| --- | --- |
| 主键 / 关联 | `id` |
| 机构 / 到诊 / 分诊 | `jgbm`、`dzdh`、`fzdh` |
| 客户 | `kunr`、`ninam` |
| 顾问 / 人员 | `fzuer`、`advxc`、`advxc_long`、`advyq`、`yyuer`、`assxc` |
| 时间 | `crtdt`、`crttm`、`fzsj` |
| 状态代码 | `dzsta`、`dzly`、`dymd`、`dztyp`、`fzsta`、`jcsta` |
| 业务备注 | `remark_dz` |

如果还希望保留少量辅助分析信息，可以加上：

- `kulvl_dq`
- `kusex`
- `kutyp_dq`
- `kut30_dq`
- `kusta_dq`
- `kusrc`
- `kusrc2`
- `jgks`
- `vipkf`
- `d_fzuer`
- `d_vipkf`
- `ddsc`
- `bhkx`
- `bjzx`

### 9.4 哪些“转名字段”其实可以不用再转

如果只按 SAP HANA 与回传场景看，下面这些字段其实都不必再继续用本地兼容名：

| 当前 `visit_orders` 字段 | 更建议直接使用的 SAP HANA 字段 |
| --- | --- |
| `khlx` | `kutyp_dq` |
| `khlx_t30` | `kut30_dq` |
| `khlx2` | `kusta_dq` |
| `vipkf` | `vipkf` |
| `qdly1_txt` | `kusrc` |
| `qdly2_txt` | `kusrc2` |
| `jgks` | `jgks` |

另外两组字段也更适合最终回归 SAP HANA 原名，但它们当前带有一点兼容逻辑，不能简单做一对一改名：

| 当前 `visit_orders` 字段 | 更接近的 SAP HANA 字段 | 备注 |
| --- | --- | --- |
| `fzr_id_dq` | `d_fzuer` | 当前实现里带 `d_fzuer or fzuer` fallback |
| `d_vipkf` | `d_vipkf` | 当前实现里带 `d_vipkf or vipkf` fallback |
| `customer_gender` | `kusex` | 当前已经从 `F/M` 归一成 `男/女` |
| `advxc_long` | `ADVXC_LONG` | 来自 `FZDATA`，建议后续如果重构结构，可直接按 SAP 命名 |

### 9.5 哪些字段从“回传闭环”角度看不是必须落在表里

下面这批字段并不是“当前咨询单回传”和“未来标签回传最小闭环”所必需的：

| 字段 | 原因 |
| --- | --- |
| `dzseg` | 技术行号；如果 `fzdh` 稳定存在，可不再作为业务关键字段 |
| `advyq_name` | 姓名展示字段 |
| `fzr_name_dq` | 姓名展示字段 |
| `sjrq` | 旧兼容数据日期 |
| `fzrq` | 旧兼容分诊日期 |
| `jdrq` | 旧兼容建档日期 |
| `customer_birthday` | 当前无稳定 SAP HANA 来源 |

### 9.6 关于中文翻译字段的最终建议

如果只按“数据事实”和“回传闭环”看，下面这些中文字段**不是必须存表**：

- `dzsta_txt`
- `dzly_txt`
- `dymd_txt`
- `dztyp_txt`
- `fzsta_txt`
- `jcsta_txt`

但它们对下面这些场景仍然很有价值：

- 管理后台页面展示
- 录音匹配说明
- 人工复核
- 导出表格

因此更合理的长期方案通常是二选一：

1. 继续把这些 `*_txt` 存在 `visit_orders`
2. 不再存表，但在接口层 / 前端层统一根据码表实时翻译

从“数据模型更干净”的角度，我更推荐第 2 种。
