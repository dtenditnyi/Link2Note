"""Что Gemini должен вернуть: структурированный конспект (structured output).

Необязательные поля — только через default_factory: pydantic не пишет `default` в JSON-схему,
а google-genai 3.0 пропускает `default` вложенных моделей в запрос (Gemini его не поддерживает).
Списки необязательны, потому что модель иногда пропускает пустые поля-списки.
"""

from typing import Literal

from pydantic import BaseModel, Field


class Shortcut(BaseModel):
    action: str = Field(description="Что делает сочетание")
    windows: str = Field(
        default_factory=str, description="Сочетание для Windows/Linux, как в видео"
    )
    mac: str = Field(default_factory=str, description="Сочетание для macOS, если показано")


class CodeBlock(BaseModel):
    language: str = Field(
        default_factory=lambda: "bash", description="Язык для подсветки: bash, python, …"
    )
    code: str = Field(description="Команда или код ТОЧНО как в видео")
    explanation: str = Field(default_factory=str, description="Что делает, одной фразой")


class Resource(BaseModel):
    kind: Literal["book", "tool", "site", "video", "course", "other"]
    name: str
    note: str = Field(default_factory=str, description="Зачем упомянут, коротко")


class Timestamp(BaseModel):
    seconds: int = Field(description="Секунда от начала видео")
    label: str = Field(description="Что в этот момент, 2–6 слов")


class Digest(BaseModel):
    title: str = Field(description="Понятный заголовок на русском, до 80 символов")
    emoji: str = Field(description="Одно эмодзи по теме")
    tldr: str = Field(description="Суть в 1–2 предложениях")
    key_points: list[str] = Field(description="3–7 главных мыслей")
    steps: list[str] = Field(
        default_factory=list, description="Пошаговая инструкция, только если она есть в видео"
    )
    shortcuts: list[Shortcut] = Field(default_factory=list)
    code: list[CodeBlock] = Field(default_factory=list)
    resources: list[Resource] = Field(
        default_factory=list, description="Упомянутые книги, программы, сайты, курсы"
    )
    action_items: list[str] = Field(
        default_factory=list, description="1–3 конкретных действия для меня"
    )
    timestamps: list[Timestamp] = Field(default_factory=list, description="Ключевые моменты")
    tags: list[str] = Field(description="1–3 тега ТОЛЬКО из разрешённого списка")
    read_minutes: int = Field(description="Сколько минут читать конспект")
    confidence: Literal["high", "medium", "low"] = Field(
        description="Насколько уверен, что всё понял правильно"
    )
