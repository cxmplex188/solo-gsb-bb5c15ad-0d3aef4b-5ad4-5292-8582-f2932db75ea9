# Three-way JSON Merge API

面向**配置审阅**场景的三方 JSON 合并服务（Python 3.12 + FastAPI）。提交一份公共
基线 `base` 和两个修订版 `left` / `right`，服务沿 [JSON Pointer (RFC 6901)](https://www.rfc-editor.org/rfc/rfc6901)
递归合并，并给出确定的结果与冲突清单。

## 一条命令启动

```bash
docker compose up --build -d
```

服务在宿主机 **8000** 端口启动（可用环境变量 `PUBLISH_PORT` 改映射端口）：

- 健康检查：<http://localhost:8000/health>
- 合并接口：`POST http://localhost:8000/merge`
- 交互式文档（Swagger UI）：<http://localhost:8000/docs>
- OpenAPI Schema：<http://localhost:8000/openapi.json>

停止：`docker compose down`；查看日志：`docker compose logs -f`。

不使用 Compose 时，也可以直接用 Docker 启动（`-e` 可覆盖环境变量，
`-p` 调整宿主机映射端口）：

```bash
docker build -t json-merge-api .
docker run --rm -p 8000:8000 \
  -e MAX_REQUEST_SIZE_BYTES=10M \
  json-merge-api
```

镜像基于 `python:3.12-slim`，以非 root 用户运行，并内置指向 `/health` 的
`HEALTHCHECK`；容器内固定监听 `8000` 端口。

## 配置（环境变量）

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `MAX_REQUEST_SIZE_BYTES` | `10M` | 请求体大小上限。纯字节数（如 `1048576`），或带 `K` / `M` / `G`（1024 进制）后缀，如 `256K`、`10M`、`1G`。`0` 表示不限制。超限返回 `413`。 |
| `PUBLISH_PORT` | `8000` | 仅 Compose 使用：宿主机映射端口（容器内固定监听 8000）。 |

可以直接在环境里设置，或在仓库根目录放一份 `.env`（`docker compose` 会自动读取）：

```dotenv
MAX_REQUEST_SIZE_BYTES=10M
PUBLISH_PORT=8000
```

## 合并语义

沿 JSON Pointer 递归，对每个位置比较 `base` / `left` / `right`：

| 情况 | 结果 |
| --- | --- |
| 只有一侧改动（另一侧与 base 相同） | 自动采纳改动 |
| 两侧做出相同改动 | 合并该改动（只生效一次） |
| 两侧都删除同一字段/项 | 删除 |
| 一侧删除、另一侧修改（或一侧缺失时两侧新增不同值） | **保留 base**，报告冲突 |
| 两侧把同一值改成不同结果（叶子值/普通数组） | **保留 base**，报告冲突 |
| 两侧修改同一对象的不同字段 | 各自自动合并；仅冲突字段报错 |
| 未配置身份字段的数组 | 视为**整体值**（整体替换 / 整体冲突，不逐项 diff） |

冲突对象的 `kind`：

- `both_modified`：两侧把同一值改成不同结果（或 `null` 与其他值互斥修改）；
- `delete_vs_modify`：一侧删除、另一侧修改；
- `both_added`：base 中不存在该路径，两侧新增了不同的值（含身份数组中同 ID 新增内容不同）；
- `protected_change`：命中 `protectedPaths` 的位置被任一侧改动（包括单侧改动、或两侧相同改动），
  默认保留 base，等待裁决。

冲突一律保留 base 值，冲突记录包含五个字段：

```json
{ "pointer": "/servers/0/port", "kind": "both_modified",
  "base": 8080, "left": 9090, "right": 80 }
```

被删除的一侧在冲突里显示为 `null`。冲突按 `pointer`（同指针再按 `kind`）升序排列；
相同输入输出完全确定。

### 冲突裁决（conflictResolutions）

默认情况下所有冲突都保留 base 值。请求可携带可选的 `conflictResolutions`
（也接受别名 `conflict_resolutions`）逐项裁决，将**响应中报告的冲突指针**
（RFC 6901，身份数组已解析为最终下标）映射为胜出一方：

```json
"conflictResolutions": [
  { "pointer": "/servers/0/port", "resolution": "right" },
  { "pointer": "/servers/1", "resolution": "left" }
]
```

- `resolution` 取 `"base"` / `"left"` / `"right"`：胜出方的值被放入合并结果的
  冲突位置；选中已删除该位置的一侧即删除该位置（对 `both_added` 选 `"base"`
  表示该值不加入）；
- 未列出的冲突保持默认行为（保留 base）；
- 裁决适用于嵌套对象字段、身份数组的整项与项内字段、以及 `*` 通配路径解析后
  的最终指针（如 `/servers/0/disks/1/size`）；接受数组项删除后，其余裁决仍按
  **原报告指针**对应到正确的身份项，不受下标前移影响；
- 响应 `conflicts` 不受裁决影响，始终完整保留原三方值与指针，便于审计。

以下情况返回 **400**：指针格式非法、同一指针重复出现、指针在合并结果中不存在、
或指针指向的位置没有冲突。`resolution` 取其他值返回 422。

### 保护路径（protectedPaths）

有些位置（如敏感配置、审计字段）不允许被任何一侧“顺手改掉”。请求可携带可选的
`protectedPaths`（也接受别名 `protected_paths`），值为 [JSON Pointer (RFC 6901)](https://www.rfc-editor.org/rfc/rfc6901.html)
字符串数组，其中单段 `*` 通配任意一个路径段：

```json
"protectedPaths": ["/db/password", "/servers/*/port"]
```

命中路径只要发生**任一左右版本变更**，即使是单侧改动、或两侧做出相同改动，也**不会自动写入**
`merged`：服务保留 base 值，并在 `conflicts` 中追加一条三方值记录，其 `kind` 为
`protected_change`（`base` / `left` / `right` 分别为该位置的三方原值，删除侧记为 `null`）。

结构增删中的**原子性**：当**单侧**新增或删除的对象、或身份数组中单侧新增/删除的项包含
命中的受保护后代时，该结构单元**整块处理，不再逐字段拼装**——

- 未裁决时：base 中存在该单元则**整块保留**（含其未受保护的同级字段），base 中不存在则
  **整块不写入** `merged`；
- 命中的受保护路径仍逐条报告 `protected_change`（三方缺失值一律记为 `null`）；
- 对任一命中路径给出明确 `conflictResolutions` 时，胜出方的**整块**结构单元写入
  （选择删除侧即整块删除）。多个命中路径同时被裁决时，按报告指针升序的首个裁决决定整块；
- 两侧同时在场的对象/身份项仍逐字段合并，仅命中的字段被拦；两侧分别新增同一身份但内容
  不同时，也仍按字段合并。

匹配规则：

- **对象字段、身份数组项及其字段**按最终报告指针判断（身份项下标为合并完成后的最终下标，
  例如先于它的项被删除而下标前移，以最终下标为准）；
- **删除按原路径处理**：身份数组项被删除时，以它在 base 中的原始下标匹配；
- 未配置身份字段的普通数组仍是整体值，只能保护数组本身（如 `/a`），数组元素级指针
  （如 `/a/1`）不参与逐项 diff；
- 未发生任何变更的保护路径不产生冲突。

`protectedPaths` 与 `conflictResolutions` **共存**：保护冲突与普通冲突一样可以被明确裁决，
裁决胜出方覆盖保护（选择已删除的一侧即删除该位置）；**未裁决的保护冲突始终保留 base**。
响应 `conflicts` 同样不受裁决影响，始终保留原三方值。

以下情况返回 **400**：指针格式非法（不以 `/` 开头等）、同一指针重复出现（按解码后的
reference token 比较，`/a~0b` 与等价写法视为重复）。数组元素不是字符串时返回 422。

### 冲突值脱敏（redactPaths）

冲突记录的 `base` / `left` / `right` 可能包含口令、密钥等敏感值。请求可携带可选的
`redactPaths`（也接受别名 `redact_paths`），值为 [JSON Pointer (RFC 6901)](https://www.rfc-editor.org/rfc/rfc6901.html)
字符串数组（单段 `*` 通配任意一个路径段），**只改写响应 `conflicts` 中的三方值**：

- 冲突指针命中某条规则、或位于规则之下时，该冲突的三方值**整体**替换为固定字符串
  `[REDACTED]`；
- 冲突指针是某条规则的祖先时，按规则的相对余下路径在三方快照内部**递归脱敏**
  （对象按键、数组按下标、`*` 遍历全部成员），未命中的字段保持原值；
- 某侧**缺失**（被删除或从未存在，响应中显示为 `null`）的分支保持 `null`，不替换；
  真实存在的 JSON `null` 值视为存在的值，命中时同样替换为 `[REDACTED]`；
- `merged`、冲突判定与 `conflictResolutions` **始终使用原始值**：脱敏不改变合并结果，
  裁决仍按原报告指针进行。

以下情况返回 **400**：指针格式非法、同一指针重复出现（按解码后的 reference token 比较）。
数组元素不是字符串时返回 422。

### 合并溯源（includeProvenance）

审阅合并结果时，除了冲突清单，还可能需要知道**最终文档里每一处实际变更来自哪一方**。
请求可携带可选布尔字段 `includeProvenance`（也接受别名 `include_provenance`，默认 `false`）。
开启后，响应在 `merged` / `conflicts` 之外**新增**一个 `provenance` 数组；不开启时
**响应格式完全不变**（不会出现 `"provenance": null`，连键都不存在）。

`provenance` 中**每个实际发生变更的叶**（或每个对象数组的**项**）对应一条记录：

```json
{
  "pointer": "/servers/1/port",
  "source": "right",
  "adjudicated": false,
  "deleted": false
}
```

| 字段 | 含义 |
| --- | --- |
| `pointer` | 变更叶 / 数组项在**最终合并结果**中的 JSON Pointer (RFC 6901)。整个数组按最终 JSON Pointer 字符串**稳定升序**排列。 |
| `source` | 最终值的来源：`left` / `right`（单侧改动，或裁决给该侧）、`base`（未裁决冲突或 `protectedPaths` 冻结而保留 base）、`merged`（两侧做出**相同**改动，或身份数组项由两侧字段拼装）。 |
| `adjudicated` | 该结果（含删除）是否由 `conflictResolutions` 的裁决产生；自动合并与未裁决冲突为 `false`。字段级裁决会使其所在的身份数组项记录标为 `true`。 |
| `deleted` | 该叶 / 项是否已从最终文档删除。删除项**保留其在 base 中的原始指针**（不是最终下标），`source` 为执行删除的一侧；两侧都删除时为 `merged`。 |

记录粒度与合并引擎一致：

- 对象按字段递归，记录到**叶值**；未配置身份字段的普通数组是**整体值**，整条数组记一条；
- 配置了 `arrayIdentity` 的对象数组**按项**记录（一项一条，不逐字段展开），
  项下字段的来源综合为该项的 `source`，项指针使用该项在最终数组中的**最终下标**
  （先于它的项被删除导致下标前移时，以最终下标为准）；
- base 与最终文档中都不存在的位置（被拦回的新增、或 `both_added` 裁决为 `base`）
  没有最终指针，不产生记录——该次尝试仍体现在 `conflicts` 中；
- 未发生变更的叶不记录；空对象的增删本身记为一条叶记录。

溯源与其它机制严格一致：它描述的就是 `merged` 的最终结果，因此天然与 `protectedPaths`
（冻结即 `source: "base"`）、`redactPaths`（只脱敏 `conflicts`，不影响 `merged`，故不影响溯源）
以及 `conflictResolutions`（裁决结果与 `adjudicated` 标记）保持一致。

溯源示例（单侧改动、两侧相同改动、未裁决冲突各一条）：

```bash
curl -s -X POST http://localhost:8000/merge \
  -H 'Content-Type: application/json' \
  -d '{
    "base":  {"a": 1, "b": 2, "c": 3},
    "left":  {"a": 10, "b": 2, "c": 33},
    "right": {"a": 1, "b": 20, "c": 33},
    "includeProvenance": true
  }'
```

响应（`/a` 来自 left、`/b` 来自 right、`/c` 两侧相同改动记为 merged；未变更字段不出现）：

```json
{
  "merged": {"a": 10, "b": 20, "c": 33},
  "conflicts": [],
  "provenance": [
    {"pointer": "/a", "source": "left", "adjudicated": false, "deleted": false},
    {"pointer": "/b", "source": "right", "adjudicated": false, "deleted": false},
    {"pointer": "/c", "source": "merged", "adjudicated": false, "deleted": false}
  ]
}
```

身份数组示例（删除项保留原指针，存活项用最终下标）：

```bash
curl -s -X POST http://localhost:8000/merge \
  -H 'Content-Type: application/json' \
  -d '{
    "base":  {"servers": [{"id": "a", "port": 8080}, {"id": "b", "port": 80}]},
    "left":  {"servers": [{"id": "b", "port": 80}]},
    "right": {"servers": [{"id": "a", "port": 8080}, {"id": "b", "port": 81}]},
    "arrayIdentity": {"/servers": "id"},
    "includeProvenance": true
  }'
```

响应：

```json
{
  "merged": {"servers": [{"id": "b", "port": 81}]},
  "conflicts": [],
  "provenance": [
    {"pointer": "/servers/0", "source": "left", "adjudicated": false, "deleted": true},
    {"pointer": "/servers/0", "source": "right", "adjudicated": false, "deleted": false}
  ]
}
```

删除项 `a` 保留其原始指针 `/servers/0`（`deleted: true`）；存活项 `b` 删除后位于最终
下标 `0`，也显示为 `/servers/0`（`deleted: false`）——两条记录按指针相邻排列。
被接受的删除同样可以来自裁决（此时 `source` 为裁决方且 `adjudicated: true`）。

### JSON Patch（includePatch）

审阅或落地合并结果时，除了 `merged` 文档本身，还可能需要一份**把请求 `base` 变成
`merged` 的操作序列**。请求可携带可选布尔字段 `includePatch`（也接受别名
`include_patch`，默认 `false`）。开启后，响应在 `merged` / `conflicts`（以及可能的
`provenance`）之外**新增**一个 `patch` 数组；不开启时**响应格式完全不变**（不会出现
`"patch": null`，连键都不存在）。

`patch` 是一组 [JSON Patch (RFC 6902)](https://www.rfc-editor.org/rfc/rfc6902) 操作，
**按顺序应用到请求的 `base` 上，结果必须恰好等于响应的 `merged`**：

- **对象差异递归生成操作**：字段级 `add` / `remove` / `replace`，互不影响的字段各自独立；
  新增的对象子树先 `add` 一个空对象 `{}`，再逐字段 `add`，保证每条操作执行时其父对象已经存在；
  删除整个对象子树只需一条 `remove`；
- **数组作为整体替换**：无论普通数组还是经 `arrayIdentity` 逐项合并的身份数组，patch 都
  只产出一条针对整条数组的 `replace`（数组新增 / 删除同理，为整条 `add` / `remove`），
  绝不逐元素改下标；
- 标量变化、`null` 与其他值互改、对象与数组类型互换都用一条 `replace`；根文档本身被
  替换时 `path` 为空字符串 `""`；
- **JSON Pointer token 按 RFC 6901 转义**：`~` → `~0`，`/` → `~1`（对象成员按键排序遍历，
  依据是**原始未转义**键名，因此操作顺序确定）；
- 操作只描述最终的 `base → merged`：未裁决冲突保留 base 时对应位置**不产生操作**；
  冲突经 `conflictResolutions` 裁决、`protectedPaths` 冻结等结果同样如实体现在 patch 中；
  `redactPaths` 只脱敏 `conflicts`，patch 始终基于真实的 `merged` 构建，不含 `[REDACTED]`；
- `merged` 与 `base` 完全相等时 `patch` 为 `[]`。该选项可与 `includeProvenance` 同时开启。

示例：

```bash
curl -s -X POST http://localhost:8000/merge \
  -H 'Content-Type: application/json' \
  -d '{
    "base":  {
      "name": "demo",
      "db": {"host": "a", "port": 5432, "pool": 5},
      "tags": ["blue", "green"]
    },
    "left": {
      "name": "demo",
      "db": {"host": "b", "port": 5432, "pool": 5, "tls": {"enabled": true}},
      "tags": ["blue", "green", "red"]
    },
    "right": {
      "name": "demo",
      "db": {"host": "a", "port": 5433, "pool": 5},
      "tags": ["blue", "green"]
    },
    "includePatch": true
  }'
```

响应（`db/host` 取 left、`db/port` 取 right、新增的 `db/tls` 对象先建空对象再补字段、
`tags` 数组整体替换；未变更的 `name` 与 `db/pool` 不产生操作）：

```json
{
  "merged": {
    "name": "demo",
    "db": {"host": "b", "port": 5433, "pool": 5, "tls": {"enabled": true}},
    "tags": ["blue", "green", "red"]
  },
  "conflicts": [],
  "patch": [
    {"op": "replace", "path": "/db/host", "value": "b"},
    {"op": "replace", "path": "/db/port", "value": 5433},
    {"op": "add", "path": "/db/tls", "value": {}},
    {"op": "add", "path": "/db/tls/enabled", "value": true},
    {"op": "replace", "path": "/tags", "value": ["blue", "green", "red"]}
  ]
}
```

`remove` 操作按 RFC 6902 不携带 `value`，例如两侧都删除 `/old` 字段时：

```json
{"op": "remove", "path": "/old"}
```

### 对象数组与身份字段

在 `arrayIdentity` 中声明“数组指针 → 身份字段名”：

- 数组按身份字段识别同一项，进行**增 / 删 / 改**合并；
- 既有项**保持 base 中的顺序**；一侧删除而另一侧修改时保留 base 项并报告
  `delete_vs_modify`，冲突指针指向该项的最终下标（如 `/srv/1`）；
- 新增项（单侧新增，或两侧同 ID 同内容）按身份值排序后追加，排序次序：
  `null < 布尔 < 数字（按数值） < 字符串（按字典序）`；
- 指针段 `*` 通配单个位置下标，可用于嵌套数组，例如
  `"/servers/*/disks": "name"`；
- 任一侧出现非对象元素、缺少身份字段、或身份重复时，该数组自动退化为整体值比较。

## 请求与响应

`POST /merge`，字段 `base` / `left` / `right` 为任意 JSON 文档；
`arrayIdentity`（也接受别名 `array_identity`）为“指针 → 字段名”映射，可省略；
`conflictResolutions`（也接受别名 `conflict_resolutions`）为冲突裁决列表，可省略；
`protectedPaths`（也接受别名 `protected_paths`）为受保护指针数组，可省略；
`redactPaths`（也接受别名 `redact_paths`）为冲突值脱敏指针数组，可省略；
`includeProvenance`（也接受别名 `include_provenance`）为布尔值，可省略（默认 `false`），
开启后响应额外返回按最终指针排序的 `provenance` 数组（见“合并溯源”一节）；
`includePatch`（也接受别名 `include_patch`）为布尔值，可省略（默认 `false`），
开启后响应额外返回一组按顺序把 `base` 变成 `merged` 的 RFC 6902 `patch` 操作
（见“JSON Patch”一节），可与 `includeProvenance` 同时开启。

请求：

```bash
curl -s -X POST http://localhost:8000/merge \
  -H 'Content-Type: application/json' \
  -d '{
    "base":  {"servers": [{"id": "a", "port": 8080}, {"id": "b", "port": 80}]},
    "left":  {"servers": [{"id": "a", "port": 9090}, {"id": "b", "port": 80}]},
    "right": {"servers": [{"id": "a", "port": 8080}, {"id": "b", "port": 81}]},
    "arrayIdentity": {"/servers": "id"}
  }'
```

响应：

```json
{
  "merged": {
    "servers": [
      {"id": "a", "port": 9090},
      {"id": "b", "port": 81}
    ]
  },
  "conflicts": []
}
```

冲突示例（a 被 left 删除、被 right 修改）：

```json
{
  "merged": {"servers": [{"id": "a", "port": 8080}, {"id": "b", "port": 80}]},
  "conflicts": [
    {
      "pointer": "/servers/0",
      "kind": "delete_vs_modify",
      "base": {"id": "a", "port": 8080},
      "left": null,
      "right": {"id": "a", "port": 9090}
    }
  ]
}
```

指针遵循 RFC 6901 转义：`~` → `~0`，`/` → `~1`；根文档冲突的指针为空字符串 `""`。

裁决示例（沿用上面的冲突，裁决 `/servers/0` 由 right 胜出）：

```bash
curl -s -X POST http://localhost:8000/merge \
  -H 'Content-Type: application/json' \
  -d '{
    "base":  {"servers": [{"id": "a", "port": 8080}, {"id": "b", "port": 80}]},
    "left":  {"servers": [{"id": "b", "port": 80}]},
    "right": {"servers": [{"id": "a", "port": 9090}, {"id": "b", "port": 80}]},
    "arrayIdentity": {"/servers": "id"},
    "conflictResolutions": [{"pointer": "/servers/0", "resolution": "right"}]
  }'
```

响应（`merged` 中 a 取 right 的值；`conflicts` 保持原三方值不变）：

```json
{
  "merged": {"servers": [{"id": "a", "port": 9090}, {"id": "b", "port": 80}]},
  "conflicts": [
    {
      "pointer": "/servers/0",
      "kind": "delete_vs_modify",
      "base": {"id": "a", "port": 8080},
      "left": null,
      "right": {"id": "a", "port": 9090}
    }
  ]
}
```

保护路径示例（`/db/password` 即使只被 left 单侧修改也不自动写入，保留 base）：

```bash
curl -s -X POST http://localhost:8000/merge \
  -H 'Content-Type: application/json' \
  -d '{
    "base":  {"db": {"host": "a", "password": "secret"}},
    "left":  {"db": {"host": "b", "password": "rotated"}},
    "right": {"db": {"host": "b", "password": "secret"}},
    "protectedPaths": ["/db/password"]
  }'
```

响应（`host` 的单侧改动照常合并；`password` 保留 base，并报告 `protected_change`）：

```json
{
  "merged": {"db": {"host": "b", "password": "secret"}},
  "conflicts": [
    {
      "pointer": "/db/password",
      "kind": "protected_change",
      "base": "secret",
      "left": "rotated",
      "right": "secret"
    }
  ]
}
```

该冲突同样可用 `conflictResolutions` 裁决（如
`{"pointer": "/db/password", "resolution": "left"}`）；不裁决则一直保留 base。

脱敏示例（冲突的三方值被替换为 `[REDACTED]`，`merged` 仍是原始值）：

```bash
curl -s -X POST http://localhost:8000/merge \
  -H 'Content-Type: application/json' \
  -d '{
    "base":  {"db": {"host": "a", "password": "old"}},
    "left":  {"db": {"host": "b", "password": "new-left"}},
    "right": {"db": {"host": "b", "password": "new-right"}},
    "redactPaths": ["/db/password"]
  }'
```

响应：

```json
{
  "merged": {"db": {"host": "b", "password": "old"}},
  "conflicts": [
    {
      "pointer": "/db/password",
      "kind": "both_modified",
      "base": "[REDACTED]",
      "left": "[REDACTED]",
      "right": "[REDACTED]"
    }
  ]
}
```

当冲突位于脱敏规则的祖先时，按相对路径在三方快照内部递归脱敏（删除侧保持 `null`）：

```bash
curl -s -X POST http://localhost:8000/merge \
  -H 'Content-Type: application/json' \
  -d '{
    "base":  {"db": {"host": "a", "password": "old"}},
    "left":  {"db": {"host": "b", "password": "new-left"}},
    "right": {},
    "redactPaths": ["/db/password"]
  }'
```

响应（`/db` 整体冲突，`password` 字段在快照内部被脱敏，`merged` 保留 base 原值）：

```json
{
  "merged": {"db": {"host": "a", "password": "old"}},
  "conflicts": [
    {
      "pointer": "/db",
      "kind": "delete_vs_modify",
      "base": {"host": "a", "password": "[REDACTED]"},
      "left": {"host": "b", "password": "[REDACTED]"},
      "right": null
    }
  ]
}
```

## 本地开发（不使用 Docker）

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements-dev.txt
uvicorn app.main:app --reload --port 8000
pytest -q
```

## 项目结构

```
app/
  main.py         FastAPI 路由（/health、/merge）
  merger.py       三方合并引擎（JSON Pointer、身份数组、冲突建模）
  patch.py        RFC 6902 JSON Patch 生成（对象递归、数组整体替换、token 转义）
  middleware.py   请求体大小上限 ASGI 中间件
  config.py       环境变量配置与大小解析
  models.py       请求/响应 Pydantic 模型
tests/            引擎与 HTTP 层测试（pytest，含 patch 往返与随机性质测试）
Dockerfile        精简镜像（python:3.12-slim，非 root，内置 HEALTHCHECK）
docker-compose.yml 一键启动
```
