import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Config:
    bot_token: str
    llm_api_key: str
    llm_base_url: str
    llm_model: str
    llm_ask_model: str
    llm_ask_web_search: bool
    llm_timeout: float
    gemini_api_key: str | None
    gemini_model: str | None
    gemini_model_ask: str
    logs_chat_id: int
    summary_hour: int
    summary_tz: str
    db_path: str
    min_words: int
    retention_days: int
    summary_period_hours: int
    summary_daily_limit: int
    summary_bot_usernames: frozenset[str]
    ingest_token: str | None
    ingest_host: str
    ingest_port: int
    allowed_chat_ids: frozenset[int]
    allowed_user_ids: frozenset[int]
    blocked_user_ids: frozenset[int]
    sudo_user_ids: frozenset[int]

    @property
    def period_seconds(self) -> int:
        return self.summary_period_hours * 3600

    @property
    def ingest_enabled(self) -> bool:
        return bool(self.ingest_token)

    @classmethod
    def from_env(cls) -> Config:
        gemini_model = os.getenv("GEMINI_MODEL_SUMMARY") or None
        openai_api_key = os.getenv("OPENAI_API_KEY")
        freemodel_api_key = os.getenv("FREEMODEL_API_KEY")
        openrouter_api_key = os.getenv("OPENROUTER_API_KEY")
        llm_api_key = (
            openai_api_key
            or freemodel_api_key
            or openrouter_api_key
        )
        uses_openrouter = bool(
            openrouter_api_key and not openai_api_key and not freemodel_api_key
        )
        gemini_api_key = os.getenv("GEMINI_API_KEY") or None
        if not llm_api_key and not (gemini_model and gemini_api_key):
            raise RuntimeError(
                "Missing required env var: FREEMODEL_API_KEY, OPENAI_API_KEY, OPENROUTER_API_KEY, or GEMINI_API_KEY with GEMINI_MODEL_SUMMARY"
            )
        llm_model = (
            os.getenv("OPENAI_MODEL")
            or os.getenv("FREEMODEL_MODEL")
            or os.getenv("OPENROUTER_MODEL")
            or "deepseek/deepseek-chat"
        )
        return cls(
            bot_token=_required("BOT_TOKEN"),
            llm_api_key=llm_api_key or "",
            llm_base_url=(
                os.getenv("OPENAI_BASE_URL")
                or os.getenv("FREEMODEL_BASE_URL")
                or os.getenv("OPENROUTER_BASE_URL")
                or "https://openrouter.ai/api/v1"
            ),
            llm_model=llm_model,
            llm_ask_model=(
                os.getenv("OPENROUTER_MODEL_ASK") if uses_openrouter else None
            )
            or llm_model,
            llm_ask_web_search=(
                uses_openrouter
                and _parse_bool(os.getenv("OPENROUTER_ASK_WEB_SEARCH", "false"))
            ),
            llm_timeout=float(os.getenv("LLM_TIMEOUT_SECONDS", "180")),
            gemini_api_key=gemini_api_key,
            gemini_model=gemini_model,
            gemini_model_ask=os.getenv("GEMINI_MODEL_ASK", "gemini-2.5-pro"),
            logs_chat_id=int(_required("LOGS_CHAT_ID")),
            summary_hour=int(os.getenv("SUMMARY_HOUR", "10")),
            summary_tz=os.getenv("SUMMARY_TZ", "Europe/Moscow"),
            db_path=os.getenv("DB_PATH", "mr-stat.db"),
            min_words=int(os.getenv("MIN_WORDS", "10")),
            retention_days=int(os.getenv("RETENTION_DAYS", "3")),
            summary_period_hours=int(os.getenv("SUMMARY_PERIOD_HOURS", "24")),
            summary_daily_limit=int(os.getenv("SUMMARY_DAILY_LIMIT", "3")),
            summary_bot_usernames=_parse_username_set(os.getenv("SUMMARY_BOT_USERNAMES", "ainemotronbot")),
            ingest_token=os.getenv("INGEST_TOKEN") or None,
            ingest_host=os.getenv("INGEST_HOST", "0.0.0.0"),
            ingest_port=int(os.getenv("INGEST_PORT", "8080")),
            allowed_chat_ids=_parse_int_set(os.getenv("ALLOWED_CHAT_IDS", "")),
            allowed_user_ids=_parse_int_set(os.getenv("ALLOWED_USER_IDS", "")),
            blocked_user_ids=_parse_int_set(os.getenv("BLOCKED_USER_IDS", "")),
            sudo_user_ids=_parse_int_set(os.getenv("SUDO_USER_IDS", "")),
        )


def _required(name: str) -> str:
    value = os.getenv(name)
    if not value:
        raise RuntimeError(f"Missing required env var: {name}")
    return value


def _parse_int_set(raw: str) -> frozenset[int]:
    raw = raw.split("#", 1)[0]
    return frozenset(int(p) for p in raw.replace(";", ",").split(",") if p.strip())


def _parse_username_set(raw: str) -> frozenset[str]:
    raw = raw.split("#", 1)[0]
    return frozenset(
        username
        for part in raw.replace(";", ",").split(",")
        if (username := part.strip().lstrip("@").lower())
    )


def _parse_bool(raw: str) -> bool:
    return raw.strip().lower() in {"1", "true", "yes", "on"}
