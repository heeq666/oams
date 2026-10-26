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
