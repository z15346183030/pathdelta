# pathdelta

**读两份 JSON 文档，按路径列出真正变了的地方。**

`python pathdelta.py BEFORE AFTER`

只用 Python 标准库，零配置、零安装、单文件。退出码：`0` 完全一致，`1` 有变更，`2` 输入有问题，可直接放进 CI 当闸门。

---

## 一句话说清边界

**输入**：两份 JSON 文档（文件或标准输入）。
**输出**：一份按路径组织的变更清单，每条都能说明判定理由。
**边界**：只比较这两份文档的值；不写文件、不排序原文件、不改动任何输入，也不负责解释这些值在业务上是什么意思。

要解决的真实场景：接口快照、配置文件、测试夹具经常需要比一比。一个数组里插入一条记录，按下标逐个位置去比，会把后面每一条都报成改动；几百行的输出里真正有用的只有一行。pathdelta 先把没变的元素按内容钉住，只把剩下的小段拿出来细看。

---

## 快速开始

仓库自带示例，直接在根目录执行：

```bash
python pathdelta.py examples/before.json examples/after.json
```

真实输出：

```
- $.deprecated: true
+ $.features[2]: "dark-mode"
~ $.limits.rpm: 100 -> 120
+ $.limits.window: 60
~ $.port: 8080 -> 8443
+ $.region: "cn-north"
6 change(s): 3 added, 1 removed, 2 changed
```

`features` 数组中间插了一项 `dark-mode`，后面的 `gamma` 只是挪了一个位置，输出里没有出现关于它的内容。三种符号：`+` 新增、`-` 删除、`~` 改动。

加 `--explain`，每条变更多一行理由，末尾附上数组对齐记录：

```bash
python pathdelta.py --explain examples/before.json examples/after.json
```

真实输出（节选）：

```
+ $.features[2]: "dark-mode"
    why: new array element at AFTER[2], no equal element in BEFORE
~ $.limits.rpm: 100 -> 120
    why: same path, different value
6 change(s): 3 added, 1 removed, 2 changed
# alignment notes
# align $.features | 3 matched by value, 1 gap(s), +1 -0 ~0
```

---

## 常用命令

**拿实时响应和本地快照比**，`-` 表示从标准输入读。下面这条用仓库自带的例子，可以直接复制：

```bash
cat examples/after.json | python pathdelta.py examples/before.json -
```

真实输出：

```
- $.deprecated: true
+ $.features[2]: "dark-mode"
~ $.limits.rpm: 100 -> 120
+ $.limits.window: 60
~ $.port: 8080 -> 8443
+ $.region: "cn-north"
6 change(s): 3 added, 1 removed, 2 changed
```

**跳过每次都会变的字段**（接口里的时间戳、请求号）。`--ignore` 可重复，一个路径同时覆盖它下面的一切，`[*]` 匹配任意下标：

```bash
python pathdelta.py --ignore '$.limits' examples/before.json examples/after.json
```

真实输出：

```
- $.deprecated: true
+ $.features[2]: "dark-mode"
~ $.port: 8080 -> 8443
+ $.region: "cn-north"
4 change(s): 2 added, 1 removed, 1 changed
```

```bash
python pathdelta.py --ignore '$.features[*]' examples/before.json examples/after.json
```

真实输出（`$.features[2]` 的新增也被跳过了）：

```
- $.deprecated: true
~ $.limits.rpm: 100 -> 120
+ $.limits.window: 60
~ $.port: 8080 -> 8443
+ $.region: "cn-north"
5 change(s): 2 added, 1 removed, 2 changed
```

**给脚本或 CI 用**，`--json` 输出纯 JSON 数组，不夹带汇总文字：

```bash
python pathdelta.py --json examples/before.json examples/after.json
```

真实输出：

```json
[
  {
    "path": "$.deprecated",
    "op": "remove",
    "before": true
  },
  {
    "path": "$.features[2]",
    "op": "add",
    "after": "dark-mode"
  },
  {
    "path": "$.limits.rpm",
    "op": "change",
    "before": 100,
    "after": 120
  },
  {
    "path": "$.limits.window",
    "op": "add",
    "after": 60
  },
  {
    "path": "$.port",
    "op": "change",
    "before": 8080,
    "after": 8443
  },
  {
    "path": "$.region",
    "op": "add",
    "after": "cn-north"
  }
]
```

`--json --explain` 时每条记录多一个 `why` 字段，对齐记录走标准错误输出，标准输出始终可以直接被解析。

**跑测试**：

```bash
python -m unittest discover -s tests -v
```

真实输出末尾：

```
Ran 80 tests in 3.2s

OK
```

---

## 输出里几个约定

- `$` 是文档根。
- `.name` 是对象的键；名字里带点、空格或非 ASCII 字符时写成 `["..."]`。
- `[3]` 是数组下标。

- 对象的键顺序不参与判定，两份文档键写得不一样也算一致。
- 变更与新增的路径按 **AFTER 的下标** 编号，方便直接在新文档里定位；删除项用它在 **BEFORE 里的下标**，因为新文档里已经没有这个位置。
- 数字按写出来是否一致判定：`1` 与 `1.0` 算不同，`true` 与 `1` 也算不同。这样「改了什么」永远是确定的，不需要猜类型。
- 文本模式里过长的值截断到 160 字符，`--json` 不给截断。
- 输入允许 UTF-8 BOM；源文本里同一个键写了多次时，取最后一个值，并在 `--explain` 里提示。
- 退出码：`0` 没有变更、`1` 有变更、`2` 文件读不了、JSON 不合法或 `--ignore` 写法不对。

---

## 设计取舍

**为什么用路径清单，而不是一份打过补丁的文档。**
输出要能被人读、被脚本消费、被贴进工单。一份新文档看不出「哪里变了」；一串 `路径 + 动作 + 前后值` 三者都占，且天然能排序、能过滤、能计数。

**为什么数组按内容对齐，而不是按下标对齐。**
数组在业务里绝大多数是有序记录集合。插一条、删一条、换一条，其余元素只是位置移动，值没变。按内容先钉住相同元素，改动区就只剩很小一段。代价是顺序本身的变化会被记成一增一删（这是对的：顺序变了确实变了），而重复值无法靠内容区分，交给下面的改动区规则处理。

**为什么对齐分三步，而不是一步求到底。**
先钉公共首尾，绝大多数插入删除到这一步就结束，几乎不花时间；再钉两侧都只出现一次的值，这种值只有一个可能的搭档，不需要猜，用最长递增子序列保留互不交叉的一组；最后在每个剩余空隙里钉最长的连续相同片段。三步各自便宜，合起来能扛住「删一条 + 插一条 + 改几个字段 + 末位也变了」这种把公共后缀全部破坏掉的情况。

**为什么改动区里还要配对。**
空隙两侧都还有元素时，按相似度贪心配对，让「一个对象改了一个字段」落成一条字段级变更，而不是整块删除加整块新增。配不上的、或两侧毫无共同字段的，才报成整块替换或一增一删。这样输出条数少，且仍然能解释。

**为什么只留三个开关。**
`--json` 给机器，`--explain` 给人，`--ignore` 处理必然波动的字段。键名映射、忽略规则文件、输出模板、插件都不做：多一层配置，就多一处需要先看文档才能用对的地方。

**为什么给工作设上限。**
对齐窗口和配对矩阵都有规模上限，超出后退回按位置处理并照常出结果。一个被彻底打乱的超大数组不该让命令卡住；宁可少报一点细节，也要保证能跑完、能退出。

---

## 实测

本机 Python 3.13，单份文档约 472 KB、含 2000 条记录。构造的 AFTER 同时包含：中间插入一条、删除一条、改三个字段、末位字段也改动、顶层新增一个键（这个组合让公共首尾同时失效）。

- 输出 **6 条**变更：`2` 新增、`1` 删除、`3` 改动。
- 若按下标逐位比较同一批数据，会列出 **4002 条**。
- 进程内比较耗时两轮取较快值 **0.0250 s**；命令行端到端一次 **0.306 s**（含解释器启动）。
- 测试 **80 个全部通过**，其中包含 60 组随机文档的「按变更清单重建 AFTER」不变量检查。

数字只针对这份构造数据，目的是说明改动不会随数组规模放大；不同形状的文档结果会不同。

---

## 仓库内容

```
pathdelta.py            单文件实现，直接运行
tests/test_pathdelta.py 80 个测试
examples/before.json    可直接复制命令里用到的示例
examples/after.json
LICENSE                 MIT
```

需要 Python 3.9 或更高版本，不需要任何安装步骤。

---

## 许可证

MIT，见 [LICENSE](LICENSE)。
