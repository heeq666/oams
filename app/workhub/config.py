from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    database_url: str
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
