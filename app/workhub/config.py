from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    # 这是**本地默认值**，只为让「刚 clone 下来、还没有 .env」的人能 import 这个包
    # （连 17 个测试文件都收集不了的话，第一印象就是「跑不起来」）。
    # 默认值不带账号口令：需要账号的场合请设 WORKHUB_DATABASE_URL（见 .env.example），
    # 仓库里放一个长得像真凭据的串，凭证扫描器分不出它是真是假。
    # 它只在「用」的时候才生效——没配库时导入不炸、连库时才报错；本机没有这个库时
    # 正好就是后者。连接逻辑与错误处理都在 db.py，这里不做兜底、不重试、不吞错。
    database_url: str = "postgresql://127.0.0.1:5432/oams"
    # Migrations change the schema, and the runtime role is deliberately not
    # allowed to do that. Point this at a role that owns the database
    # (workhub_migrator). When it is unset migrations fall back to database_url,
    # which is what a development database with a single role wants.
    migrator_url: str | None = None
    session_ttl_hours: int = 8
    bind_host: str = "127.0.0.1"
    bind_port: int = 8000
    # The web UI hands the browser the same session token the bearer scheme uses,
    # so a cookie is exactly as strong as a token -- and revoking the session logs
    # the browser out. 'Secure' is deliberately absent: intranet deployments run
    # plain http and a Secure cookie would simply never be sent back.
    web_session_cookie: str = "workhub_session"

    # Design-stage shortcut: skip the login screen. Set this to an employee_code
    # to sign every session-less request in as that person ('1'/'true' means the
    # bootstrap administrator). It decides WHO is asking, never WHAT they may do:
    # the same grants, org-unit checks and audit trail still run. Unset it and the
    # login screen is back, which is the only correct state for a real deployment.
    dev_autologin: str | None = None

    # Which product wave the company home page reports on. Unset (the normal
    # case) means "the data centre's first wave": a hard-coded wave would go
    # stale the moment the season rolls over, and reporting on every wave at once
    # would mix a live season with last year's leftovers.
    default_wave: str | None = None

    model_config = SettingsConfigDict(env_prefix="WORKHUB_", env_file=".env", extra="ignore")


settings = Settings()
