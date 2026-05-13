import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Config:
    bot_token: str
    openrouter_api_key: str
    openrouter_model: str
    gemini_api_key: str | None
    gemini_model: str
    gemini_model_ask: str
    logs_chat_id: int
    summary_hour: int
    summary_tz: str
    db_path: str
    min_words: int
    retention_days: int
    summary_period_hours: int
    summary_daily_limit: int
    allowed_chat_ids: frozenset[int]
    allowed_user_ids: frozenset[int]
    blocked_user_ids: frozenset[int]
    sudo_user_ids: frozenset[int]

    @property
    def period_seconds(self) -> int:
        return self.summary_period_hours * 3600

    @classmethod
    def from_env(cls) -> Config:
        return cls(
            bot_token=_required("BOT_TOKEN"),
            openrouter_api_key=_required("OPENROUTER_API_KEY"),
            openrouter_model=os.getenv("OPENROUTER_MODEL", "deepseek/deepseek-chat"),
            gemini_api_key=os.getenv("GEMINI_API_KEY"),
            gemini_model=os.getenv("GEMINI_MODEL_SUMMARY", "gemini-3-flash-preview"),
            gemini_model_ask=os.getenv("GEMINI_MODEL_ASK", "gemini-2.5-pro"),
            logs_chat_id=int(_required("LOGS_CHAT_ID")),
            summary_hour=int(os.getenv("SUMMARY_HOUR", "10")),
            summary_tz=os.getenv("SUMMARY_TZ", "Europe/Moscow"),
            db_path=os.getenv("DB_PATH", "mr-stat.db"),
            min_words=int(os.getenv("MIN_WORDS", "10")),
            retention_days=int(os.getenv("RETENTION_DAYS", "3")),
            summary_period_hours=int(os.getenv("SUMMARY_PERIOD_HOURS", "24")),
            summary_daily_limit=int(os.getenv("SUMMARY_DAILY_LIMIT", "3")),
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
    return frozenset(int(p) for p in raw.replace(";", ",").split(",") if p.strip())
