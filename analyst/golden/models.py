"""The trio: a question, the SQL an analyst wrote for it, and the report they produced."""

from __future__ import annotations

from datetime import date
from pathlib import Path

import yaml
from pydantic import BaseModel, Field


class Trio(BaseModel):
    id: str
    question: str
    aliases: list[str] = Field(default_factory=list)
    tags: list[str] = Field(default_factory=list)
    tables: list[str] = Field(default_factory=list)
    analyst_notes: str = ""
    sql: str
    report: str = ""
    verified_at: date | None = None
    source: str = "seed"

    def embed_text(self) -> str:
        """What gets embedded: the business question, its aliases and tags — never the SQL.

        Questions are matched against questions. Embedding the SQL would rank on similarity of syntax,
        which has nothing to do with whether the analyst's reasoning applies here.
        """
        return " | ".join([self.question, *self.aliases, " ".join(self.tags)])

    @classmethod
    def from_yaml(cls, path: Path) -> "Trio":
        data = yaml.safe_load(path.read_text())
        data.setdefault("id", path.stem)
        return cls.model_validate(data)

    def to_yaml(self, path: Path) -> None:
        data = self.model_dump(mode="json", exclude_none=True)
        path.write_text(yaml.safe_dump(data, sort_keys=False, allow_unicode=True, width=110))


def load_trios(golden_dir: Path) -> list[Trio]:
    return [Trio.from_yaml(path) for path in sorted((golden_dir / "trios").glob("*.yaml"))]
