import os
from dataclasses import dataclass

from dotenv import load_dotenv

load_dotenv()


@dataclass(frozen=True)
class ProviderConfig:
    provider: str = os.getenv("AI_ANALYSIS_PROVIDER", "auto").strip().lower()
    openai_api_key: str = os.getenv("OPENAI_API_KEY", "")
    openai_model: str = os.getenv("OPENAI_MODEL", "gpt-4o-mini")
    gemini_api_key: str = os.getenv("GEMINI_API_KEY", "")
    gemini_model: str = os.getenv("GEMINI_MODEL", "gemini-1.5-flash")

    @classmethod
    def from_env(cls):
        return cls(
            provider=os.getenv("AI_ANALYSIS_PROVIDER", "auto").strip().lower(),
            openai_api_key=os.getenv("OPENAI_API_KEY", ""),
            openai_model=os.getenv("OPENAI_MODEL", "gpt-4o-mini"),
            gemini_api_key=os.getenv("GEMINI_API_KEY", ""),
            gemini_model=os.getenv("GEMINI_MODEL", "gemini-1.5-flash"),
        )

    def selected_provider(self):
        if self.provider == "fallback":
            return "fallback"
        if self.provider == "openai" and self.openai_api_key:
            return "openai"
        if self.provider == "gemini" and self.gemini_api_key:
            return "gemini"
        if self.provider == "auto":
            if self.openai_api_key:
                return "openai"
            if self.gemini_api_key:
                return "gemini"
        return "fallback"

    def selected_model(self):
        provider = self.selected_provider()
        if provider == "openai":
            return self.openai_model
        if provider == "gemini":
            return self.gemini_model
        return "fallback"

    def provider_label(self):
        provider = self.selected_provider()
        if provider == "openai":
            return "ChatGPT API"
        if provider == "gemini":
            return "Gemini API"
        return "규칙 기반 fallback"
