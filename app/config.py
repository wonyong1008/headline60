from pathlib import Path
from urllib.parse import urlparse

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

ROOT = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="HEADLINE_", env_file=ROOT / ".env", extra="ignore"
    )
    ollama_url: str = "http://127.0.0.1:11434"
    model: str = "qwen3:4b"
    voice: str = "Yuna"
    tts_rate: int = 185
    encoder: str = "h264_videotoolbox"
    port: int = 8060
    # Default is this Mac only. Set HEADLINE_HOST=0.0.0.0 to open the admin page on the LAN;
    # HEADLINE_ALLOWED_HOSTS then lists the LAN IP/name other devices type in the address bar.
    host: str = "127.0.0.1"
    allowed_hosts: list[str] = []
    # Uploads are refused unless the authorized account owns exactly this channel (when set).
    youtube_channel_id: str = ""
    font: Path = Path("/System/Library/Fonts/AppleSDGothicNeo.ttc")
    root: Path = ROOT
    tts_profile: dict | None = None

    @field_validator("ollama_url")
    @classmethod
    def local_only(cls, value: str) -> str:
        parsed = urlparse(value)
        if parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "localhost", "::1"}:
            raise ValueError("Ollama URL은 로컬 HTTP 주소만 허용합니다.")
        return value.rstrip("/")

    @field_validator("encoder")
    @classmethod
    def encoder_allowed(cls, value: str) -> str:
        if value not in {"h264_videotoolbox", "libx264"}:
            raise ValueError("지원 인코더: h264_videotoolbox, libx264")
        return value

    @property
    def outputs(self) -> Path:
        return self.root / "outputs"

    @property
    def data(self) -> Path:
        return self.root / "data"


settings = Settings()
