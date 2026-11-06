"""Extension contracts; no untested anchors or neural voices are advertised as available."""
from typing import Literal

from pydantic import BaseModel, Field

from app.tts.profiles import VoiceProfile

__all__ = ["AnchorProfile", "VoiceProfile"]


class AnchorProfile(BaseModel):
    anchor_id: str
    name: str
    gender: str
    reference_images: list[str] = Field(default_factory=list)
    base_video: str | None = None
    voice_profile_id: str
    hairstyle: str = ""
    appearance_prompt: str = ""
    default_studio: str = ""
    outfit_presets: list[str] = Field(default_factory=list)
    default_style_level: Literal[1, 2, 3] = 2
    validated: bool = False
