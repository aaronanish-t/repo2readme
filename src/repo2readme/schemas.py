"""Structured-output schemas for each LLM stage. Every claim carries evidence we can check."""

from __future__ import annotations

from pydantic import BaseModel, Field

# ---------- map pass: one call per chunk of files ----------


class SymbolNote(BaseModel):
    name: str = Field(description="Exact symbol name as it appears in the file's symbol list.")
    role: str = Field(description="What this symbol does, in one short sentence.")


class Evidence(BaseModel):
    statement: str = Field(
        description="A factual statement about the file. Wrap identifiers, literals and paths in backticks."
    )
    start_line: int = Field(description="First line (1-based) of the code that supports the statement.")
    end_line: int = Field(description="Last line (1-based, inclusive) of the supporting code.")


class FileSummary(BaseModel):
    path: str
    purpose: str = Field(description="One or two sentences: what this file is for.")
    key_symbols: list[SymbolNote]
    facts: list[Evidence] = Field(
        description="Notable behaviors: entrypoints, CLI commands, HTTP routes, config/env read, external services, storage, algorithms."
    )
    is_entrypoint: bool = Field(description="True if this file starts a program, server, CLI, or exports the public API.")


class ChunkSummary(BaseModel):
    files: list[FileSummary]


# ---------- reduce pass: directories -> module summaries ----------


class ModuleSummary(BaseModel):
    path: str = Field(description="Directory path this summary covers ('' for repo root).")
    summary: str = Field(description="2-4 sentences on this module's responsibility.")
    responsibilities: list[str]
    key_files: list[str] = Field(description="Most important file paths in this module, exactly as given.")


class ModuleBatch(BaseModel):
    modules: list[ModuleSummary]


# ---------- final synthesis ----------


class Component(BaseModel):
    id: str = Field(description="Short identifier: letters, digits, underscore.")
    label: str = Field(description="Human-readable name shown in the diagram.")
    description: str
    paths: list[str] = Field(description="Directories or files (exact repo paths) that make up this component.")


class Feature(BaseModel):
    text: str = Field(description="One capability of the project, stated concretely. Backtick identifiers.")
    evidence_paths: list[str] = Field(description="Repo paths that implement this feature.")


class StructureEntry(BaseModel):
    path: str
    description: str


class ConfigEntry(BaseModel):
    name: str = Field(description="Environment variable or config key, exactly as it appears in the facts.")
    description: str


class ReadmeDraft(BaseModel):
    title: str
    tagline: str = Field(description="One sentence.")
    overview: str = Field(description="Markdown paragraphs: what it is, who it's for, how it works at a high level.")
    features: list[Feature]
    architecture: str = Field(description="Markdown: how the components interact. Refer to components by label.")
    components: list[Component] = Field(description="3-9 components covering the important code.")
    installation: str = Field(
        description="Markdown with fenced shell blocks. Use only commands supported by the manifest facts. Empty string if unknown."
    )
    usage: str = Field(description="Markdown with examples grounded in entrypoints, CLI commands, routes, or public API.")
    configuration: list[ConfigEntry]
    project_structure: list[StructureEntry]
    development: str = Field(description="Markdown: tests, lint, build commands from the facts. Empty string if none.")
