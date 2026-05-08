# SAP HANA 到诊分诊单推送接口

用于由 SAP HANA 侧直接向智能工牌系统推送最新的到诊分诊单数据，替代原先从远程 `cur.visit_order` 主动拉取的方式。

## 基本信息

- 固定完整地址：`POST http://192.168.5.162:8000/api/v1/visit-orders/push`
- 相对路径：`POST /api/v1/visit-orders/push`
- 协议：`HTTP / HTTPS`
- 数据格式：`application/json`
- 鉴权方式：请求头 `X-API-Key`
- 字符编码：`UTF-8`

当前对接说明：

- SAP HANA 当前请直接使用 `http://192.168.5.162:8000/api/v1/visit-orders/push`
- 该地址基于当前服务器局域网地址 `192.168.5.162` 和应用监听端口 `8000`
- 如后续切换到正式域名或 `80/443` 反向代理，对外地址将再单独通知

完整示例：

```http
POST /api/v1/visit-orders/push HTTP/1.1
Host: 192.168.5.162:8000
Content-Type: application/json
X-API-Key: <由我方单独提供>
```

## 鉴权配置

服务端通过环境变量 `SAP_HANA_PUSH_API_KEY` 校验调用方身份。

正式环境说明：

```env
SAP_HANA_PUSH_API_KEY=<由我方单独提供>
```

对接约定：

- `X-API-Key` 由我方单独线下提供，不写入对外文档正文
- SAP HANA 调用时需原样透传，不要额外加引号或做 Base64 编码
- 如需轮换，我方会先生成新 key，再通知对端切换

### API Key 生成建议

建议使用高强度随机字符串，长度不低于 32 字节。示例命令：

```bash
python3 -c "import secrets; print(secrets.token_urlsafe(32))"
```

建议：

- 测试环境和正式环境使用不同 key
- 不要把真实 key 写入邮件、群聊截图或共享文档
- 如怀疑泄露，应立即重新生成并替换

## 请求说明

接口同时支持两种请求体：

- 单条对象推送
- 数组批量推送

### 单条示例

```json
{
  "JGBM": "6101",
  "DZDH": "DZ2026041501",
  "YYDH": "YY2026041501",
  "CRTDT": "20260415",
  "CRTTM": "093015",
  "DZSTA": "C",
  "KUNR": "70001234",
  "NINAM": "李女士",
  "KUSEX": "F",
  "KULVL_DQ": "V1",
  "KUTYP_DQ": "V",
  "KUT30_DQ": "V",
  "KUSTA_DQ": "V1",
  "DZLY": "Y",
  "DYMD": "A",
  "DZTYP": "1",
  "REMARK_DZ": "面部年轻化咨询",
  "JGKS": "MRYX",
  "FZUER": "81034062",
  "FZUER_LONG": "杜娟",
  "VIPKF": "82000001",
  "D_FZUER": "81034062",
  "D_VIPKF": "82000001",
  "ADVYQ": "81030001",
  "KUSRC": "D01",
  "KUSRC2": "D0101",
  "YYUER": "82010001",
  "BJZX": "",
  "BHKX": "",
  "FZDATA": [
    {
      "FZDH": "FZ20260415001",
      "ADVXC": "81034062",
      "ADVXC_LONG": "杜娟",
      "ASSXC": "81030088",
      "FZSJ": "094500",
      "FZSTA": "A",
      "DDSC": "15",
      "JCSTA": "N"
    }
  ]
}
```

### 批量示例

```json
[
  {
    "JGBM": "6101",
    "DZDH": "DZ2026041501",
    "CRTDT": "20260415",
    "CRTTM": "093015",
    "FZDATA": []
  },
  {
    "JGBM": "6101",
    "DZDH": "DZ2026041502",
    "CRTDT": "20260415",
    "CRTTM": "101500",
    "FZDATA": []
  }
]
```

## 顶层字段

| 字段 | 含义 | 类型 | 是否必填 | 说明 |
| --- | --- | --- | --- | --- |
| `JGBM` | 机构 | string | 是 | 作为推送快照的机构编码 |
| `DZDH` | 到诊单号 | string | 是 | 与 `JGBM` 一起作为快照唯一键 |
| `YYDH` | 预约单号 | string | 否 | |
| `CRTDT` | 建档日期 | string | 否 | 推荐 `YYYYMMDD` |
| `CRTTM` | 建档时间 | string | 否 | 推荐 `HHMMSS` |
| `DZSTA` | 到诊状态 | string | 否 | `1/A/C/D` |
| `KUNR` | 客户号 | string | 否 | |
| `NINAM` | 客户姓名 | string | 否 | |
| `KUSEX` | 性别 | string | 否 | `M/F`，系统会归一化为 `男/女` |
| `KULVL_DQ` | 当前会员星级 | string | 否 | |
| `KUTYP_DQ` | 当前客户类型(T0) | string | 否 | `Q/V` |
| `KUT30_DQ` | 当前客户类型(T30) | string | 否 | `Q/V` |
| `KUSTA_DQ` | 当前客户类型2 | string | 否 | `Q1/Q2/Q3/V1` |
| `DZLY` | 到诊来源 | string | 否 | `Y/N` |
| `DYMD` | 到院目的 | string | 否 | `A/B/C/D/X/Z` |
| `DZTYP` | 到诊类型 | string | 否 | `1/2/3/4/5/Z` |
| `REMARK_DZ` | 到诊需求 | string | 否 | |
| `JGKS` | 机构科室 | string | 否 | |
| `FZUER` | 美学顾问编码 | string | 否 | |
| `FZUER_LONG` | 美学顾问姓名 | string | 否 | |
| `VIPKF` | 客服编码 | string | 否 | |
| `D_FZUER` | 美学顾问编码-机构层 | string | 否 | |
| `D_VIPKF` | 客服编码-机构层 | string | 否 | |
| `ADVYQ` | 院前美学顾问 | string | 否 | |
| `KUSRC` | 渠道来源1 | string | 否 | |
| `KUSRC2` | 渠道来源2 | string | 否 | |
| `YYUER` | 被预约人 | string | 否 | |
| `BJZX` | 不见咨询标识 | string | 否 | 原样落入 SAP HANA 推送快照表 |
| `BHKX` | 补划扣标识 | string | 否 | |
| `FZDATA` | 分诊子表 | array | 否 | 见下方子表说明 |

## `FZDATA` 子表字段

| 字段 | 含义 | 类型 | 是否必填 | 说明 |
| --- | --- | --- | --- | --- |
| `FZDH` | 分诊单号 | string | 否 | |
| `ADVXC` | 现场美学顾问编码 | string | 否 | |
| `ADVXC_LONG` | 现场美学顾问姓名 | string | 否 | |
| `ASSXC` | 美学顾问助理 | string | 否 | |
| `FZSJ` | 分诊时间 | string | 否 | 推荐 `HHMMSS` |
| `FZSTA` | 分诊状态 | string | 否 | `1/A` |
| `DDSC` | 等待时长（分） | string/number | 否 | 会按字符串落库 |
| `JCSTA` | 成交状态 | string | 否 | `N/Y/Z` |

## 域值说明

### `DZSTA`

| 值 | 含义 |
| --- | --- |
| `1` | 未分诊 |
| `A` | 已确认 |
| `C` | 已分诊 |
| `D` | 已取消 |

### `KUSEX`

| 值 | 含义 |
| --- | --- |
| `M` | 男 |
| `F` | 女 |

### `KUTYP_DQ`

| 值 | 含义 |
| --- | --- |
| `Q` | 潜客/新客 |
| `V` | 会员/老客 |

### `KUT30_DQ`

| 值 | 含义 |
| --- | --- |
| `Q` | 潜客/新客 |
| `V` | 会员/老客 |

### `KUSTA_DQ`

| 值 | 含义 |
| --- | --- |
| `Q1` | 建档未上门 |
| `Q2` | 上门未成交 |
| `Q3` | 体验会员 |
| `V1` | 付费会员 |

### `DZLY`

| 值 | 含义 |
| --- | --- |
| `Y` | 已预约 |
| `N` | 未预约 |

### `DYMD`

| 值 | 含义 |
| --- | --- |
| `A` | 咨询 |
| `B` | 治疗 |
| `C` | 手术 |
| `D` | 复查 |
| `X` | 未到院购买 |
| `Z` | 其他 |

### `DZTYP`

| 值 | 含义 |
| --- | --- |
| `1` | 初诊 |
| `2` | 复诊 |
| `3` | 再咨 |
| `4` | 诊疗 |
| `5` | 未到院购买 |
| `Z` | 其他 |

### `FZSTA`

| 值 | 含义 |
| --- | --- |
| `1` | 待接诊 |
| `A` | 已接诊 |

### `JCSTA`

| 值 | 含义 |
| --- | --- |
| `N` | 未成交 |
| `Y` | 已成交 |
| `Z` | 已治疗 |

### `JGKS`

| 值 | 含义 |
| --- | --- |
| `JGKS01` | 口腔科 |
| `JGKS02` | 皮肤科 |
| `JGKS03` | 外科 |
| `JGKS04` | 微整科 |
| `JGKS05` | 中医 |
| `JGKS06` | 纹绣 |
| `JGKS07` | 会籍 |
| `JGKS08` | 毛发移植科 |
| `JGKS09` | 非手术 |
| `JGKS10` | 私密中心 |
| `JGKS11` | 纤体中心 |
| `JGKS12` | 植发中心 |
| `JGKS13` | 形体私密中心 |
| `JGKS14` | SPA中心 |

## 返回格式

接口统一返回以下结构：

| 字段 | 含义 |
| --- | --- |
| `STATE` | `S` 成功，`E` 失败 |
| `MSG` | 处理结果说明 |

### 成功示例

HTTP 状态码：`200`

```json
{
  "STATE": "S",
  "MSG": "接收成功：1 条，新增 1 条，更新 0 条"
}
```

### 常见失败示例

#### 1. 鉴权失败

HTTP 状态码：`401`

```json
{
  "STATE": "E",
  "MSG": "X-API-Key 无效"
}
```

#### 2. 参数错误

HTTP 状态码：`400`

```json
{
  "STATE": "E",
  "MSG": "请求参数校验失败: DZDH Field required"
}
```

#### 3. 服务端未配置密钥

HTTP 状态码：`503`

```json
{
  "STATE": "E",
  "MSG": "SAP HANA 推送接口尚未配置 X-API-Key"
}
```

## 服务端处理规则

当前版本的落库规则如下：

1. 所有推送数据都会先写入独立表 `sap_hana_visit_orders`，不再直接写旧 `visit_orders`。
2. 以 `JGBM + DZDH` 作为快照唯一键做新增或更新。
3. `FZDATA` 会按原始数组整体保存，不会拆成旧版 `visit_orders` 的分诊字段结构。
4. 服务端会同时保留完整原始 JSON 报文，便于后续追溯、对账和重放。
5. 当前接口仅负责接收和沉淀 SAP HANA 原始快照，暂不自动同步本地客户表和接诊记录表。

## 对接建议

1. 建议 SAP HANA 在到诊单发生创建、确认、分诊、成交状态变更时都重新推送一次最新快照。
2. 建议日期统一使用 `YYYYMMDD`，时间统一使用 `HHMMSS`。
3. 建议 `DZDH` 在业务上保持唯一稳定，不要复用。
4. 如果一次推送多条，建议控制单次批量大小，避免单次报文过大。
5. 如需后续扩展字段，请保持现有字段不变，仅做追加。

## 当前已支持的行为

- 支持单条推送
- 支持数组批量推送
- 支持根据 `JGBM + DZDH` 自动 upsert
- 支持保留原始 JSON 报文与 `FZDATA` 明细
- 支持统一 `STATE/MSG` 返回
