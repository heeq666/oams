# 在你电脑上跑起来（本地试用）

这份说明让这套系统在你自己的电脑上跑起来，并点开三个页面。全部是本机地址，不对外网开放。

## 一次性准备

1. 确认 Docker 已启动（桌面版打开即可）。
2. 在项目目录执行（只做一次）：

```sh
docker run -d --name workhub-live \
  -e POSTGRES_PASSWORD=change-me -e POSTGRES_DB=workhub \
  -p 55460:5432 postgres:16-alpine

export WORKHUB_DATABASE_URL=postgresql://postgres:change-me@127.0.0.1:55460/workhub
export WORKHUB_MIGRATOR_URL=$WORKHUB_DATABASE_URL

.venv/bin/python -m workhub.cli migrate
.venv/bin/python -m workhub.cli bootstrap-admin \
  --name "系统管理员" --employee-code ADMIN001 --org-code COMPANY --org-name "公司"
```

把 bootstrap 打印的 `member_id` 记下来，下一步要用。

## 准备一个部门

```sh
export WORKHUB_ADMIN_MEMBER_ID=<上一步的 member_id>
.venv/bin/python scripts/load_department_data.py data/live-design-department.json --apply
.venv/bin/python scripts/seed-live-interface.py
```

第二个脚本会把[设计部打样进度界面](data/live-design-interface.json)启用，作为你点开就能看的示例。

## 启动服务

```sh
export WORKHUB_DATABASE_URL=postgresql://postgres:change-me@127.0.0.1:55460/workhub
.venv/bin/python -m uvicorn workhub.api:app --host 127.0.0.1 --port 8000
```

保持这个终端开着。

## 设计开发阶段：免登录

现在还在设计开发阶段，每天反复签登录链接只是挡自己。`scripts/start-local-trial.sh` 启动时默认打开免登录（等价于 `export WORKHUB_DEV_AUTOLOGIN=1`）：没有登录态时服务按 `ADMIN001` 认人，页面右下角常驻一枚「开发模式 · 免登录 · 姓名」角标，「退出登录」也会明确告诉你退不掉。

它只决定「我是谁」，不决定「我能干什么」：权限、部门范围、表单令牌、审计一条都不少，所以看到的页面和登录后完全一致。

恢复真实登录：

```sh
WORKHUB_DEV_AUTOLOGIN=0 ./scripts/start-local-trial.sh
```

想让免登录认别人就写工号，例如 `WORKHUB_DEV_AUTOLOGIN=DESIGN001`。工号不存在或已离职时会停在登录页并给出提示，不会静默当成管理员。

### 右下角角标可以换身份

点开那枚角标就是身份切换菜单：列出库里所有在职成员，按权限强弱排（系统管理员 → 部门管理员 → 普通员工），右边标出这个人的实际层级（由 grants 推出来，不是岗位名）。点一个就换成他看这一页，不用改环境变量、不用重启服务；切换结果记在 `workhub_dev_worker` cookie 里，12 小时有效，菜单底部还能「恢复默认身份」回到启动参数指定的那个。

调试权限时很好用：同一个页面，管理员看到的导航和普通员工不一样，点两下就能对照。免登录关掉后这个菜单和 `POST /dev/identity` 一起消失（返回 404），cookie 也不会再让人登进来。

## 三级角色与两个页面的可见范围

只有三级，层级由 grants 推出来（`security.admin_level`），不看岗位名：

| 层级 | 判定 | 典型是谁 |
| --- | --- | --- |
| 系统管理员 | 持有 `workforce.grants:manage`（也就是能打开设置页的那条） | 全公司一名 |
| 部门管理员 | 持有任意一条 `*:manage`（工作项 / 成员 / 界面 / 岗位 / 任岗 / 部门），但没有 `grants:manage` | 每个部门默认一名；系统管理员也可以单独授予某个员工 |
| 普通员工 | 其余所有人（只有 read / submit / review 这类只读或本人权限） | 大多数成员 |

「审页面」（`workforce.interfaces:review`）是**只读**权限，不会把人提到部门管理员。

两个页面对齐这套层级：

- **进度中心**：所有角色都能看到「公司总览」（全公司汇总数字）、自己所属部门的明细；管理员（系统管理员与部门管理员）能看到所有部门的明细。页面不写「你是 X，能看到 Y」这类话：看得到的部门就是答案，没显示的就是看不到。
- **汇报中心**：普通员工只看到自己提交的汇报（每个人取自己最新的一条，不会被同事更新的条目挤掉）；部门管理员看到自己负责的部门的汇报；系统管理员看到全部。

## 拿到登录链接

免登录开着时这一步可以跳过。需要真实会话时，另开一个终端：

```sh
export WORKHUB_DATABASE_URL=postgresql://postgres:change-me@127.0.0.1:55460/workhub
.venv/bin/python -m workhub.cli issue-token --member-id <member_id>
```

浏览器打开：

```
http://127.0.0.1:8000/login?token=<access_token>&next=/interfaces
```

## 三个页面

| 页面 | 地址 | 用途 |
|---|---|---|
| 部门界面 | /interfaces | 部门成员打开本部门界面填写 |
| 填写页 | /interfaces/<id> | 按描述渲染出的表单 |
| 界面制作 | /setup/interfaces | 粘贴 AI 产出的描述，校验并启用 |
| 汇报汇总 | /interfaces/board | 管理层按部门查看最近汇报 |

## 停掉服务

- 停止网站：在运行 uvicorn 的终端按 Ctrl+C。
- 停止数据库：`docker stop workhub-live`，下次 `docker start workhub-live` 即可，数据保留。
- 完全重来：`docker rm -f workhub-live`，然后从"一次性准备"重新开始。

## 注意

- 登录链接等同临时密码，不要发到群里；有效期 8 小时（默认）。
- 这是本机试用环境，数据库只监听本机端口，不要暴露到公网。
- 现在的账号仍需管理员签发链接，账号中心尚未接入。
