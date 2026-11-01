# 第三方组件与许可证

本文件列出本项目随仓库分发的第三方代码，以及运行/开发依赖的许可证。

> 本文件是清单，不是法律意见。正式发布前请律师复核。

## 一、随仓库分发的第三方代码

### htmx 1.9.12 · 0BSD

- 文件：`app/oams/static/vendor/htmx.min.js`
- 版本：**1.9.12**（文件内 `version:"1.9.12"`）
- 许可证：**0BSD（Zero-Clause BSD）**
- 来源：<https://unpkg.com/htmx.org@1.9.12/>（上游仓库 <https://github.com/bigskysoftware/htmx>，标签 `v1.9.12`）
- 说明：仓库里的这个文件**没有随带许可文本**，所以把上游 `LICENSE` 原文抄录于此。上游该文件本身不含版权人一行。

原文（逐字抄录自 htmx 1.9.12 的 `LICENSE`）：

```text
Zero-Clause BSD
=============

Permission to use, copy, modify, and/or distribute this software for
any purpose with or without fee is hereby granted.

THE SOFTWARE IS PROVIDED “AS IS” AND THE AUTHOR DISCLAIMS ALL
WARRANTIES WITH REGARD TO THIS SOFTWARE INCLUDING ALL IMPLIED WARRANTIES
OF MERCHANTABILITY AND FITNESS. IN NO EVENT SHALL THE AUTHOR BE LIABLE
FOR ANY SPECIAL, DIRECT, INDIRECT, OR CONSEQUENTIAL DAMAGES OR ANY
DAMAGES WHATSOEVER RESULTING FROM LOSS OF USE, DATA OR PROFITS, WHETHER IN
AN ACTION OF CONTRACT, NEGLIGENCE OR OTHER TORTIOUS ACTION, ARISING OUT
OF OR IN CONNECTION WITH THE USE OR PERFORMANCE OF THIS SOFTWARE.
```

0BSD 不要求保留版权声明，也不要求衍生作品同样开源；与 AGPL-3.0 兼容。

## 二、运行依赖

| 包 | 声明范围（`pyproject.toml`） | 验证版本 | 许可证 |
|---|---|---|---|
| fastapi | `>=0.115,<1` | 0.142.4 | MIT |
| uvicorn[standard] | `>=0.34,<1` | 0.54.0 | BSD-3-Clause |
| psycopg[binary,pool] | `>=3.2,<4` | psycopg 3.3.6 / psycopg-binary 3.3.6 / psycopg-pool 3.3.3 | **LGPL-3.0-only** |
| pydantic-settings | `>=2.7,<3` | 2.15.0 | MIT |
| jinja2 | `>=3.1,<4` | 3.1.6 | BSD（上游 classifier 写「BSD License」） |
| python-multipart | `>=0.0.20` | 0.0.32 | Apache-2.0 |
| pyyaml | `>=6,<7` | 6.0.3 | MIT |
| mcp | `>=2.3,<3` | 2.3.0 | MIT |

「验证版本」取自仓库 `uv.lock`；`uv.lock` 未覆盖的包（jinja2、python-multipart、mcp）取本机已安装版本。许可证以各包 `*.dist-info/METADATA` 的 `License-Expression` 字段为准，没有该字段的取其 `Classifier: License ::` 行。

## 三、开发依赖

| 包 | 声明范围 | 验证版本 | 许可证 |
|---|---|---|---|
| pytest | `>=8,<10` | 9.1.1 | MIT |
| httpx | `>=0.27,<1` | 0.28.1 | BSD-3-Clause |

开发依赖不随发布产物分发。

## 四、结论

- **没有 GPL / AGPL 强传染依赖**：运行与开发依赖里没有 GPL 或 AGPL 授权者。
- **psycopg 系列是 LGPL-3.0-only**（弱传染，属 GPL 家族）。它作为**未修改的库被 import 使用**，不要求本项目公开源码；本项目对它的使用方式是动态链接式的库调用，符合 LGPL 的条件。
- **LGPLv3 与 AGPLv3 可以共存**：LGPLv3 明确允许把作品按 GPLv3（进而 AGPLv3）分发，两者之间不存在冲突。
- 商业授权**不改变**这些第三方组件的授权条件：买商业授权得到的是本项目代码的闭源使用许可，第三方组件仍按上表的许可证约束。
