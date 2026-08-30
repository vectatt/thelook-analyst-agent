"""Golden bucket index: find the analyses a human already wrote for questions like this one.

Embeddings: Gemini `gemini-embedding-001` via google-genai. Store: LanceDB on disk (embedded, no server).
Search: cosine similarity on the embedded *question*, plus a small keyword boost so a short question
that names a metric ("AOV by channel") still finds the right trio.

Resilience: query embeddings are cached; if the embedding API is unavailable for any reason (quota,
network, outage) search degrades to keyword-only scoring instead of failing the turn, and the index
keeps serving its last good table.
"""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass
from pathlib import Path

import lancedb
import pyarrow as pa
from google import genai
from google.genai import types

from analyst.config import settings
from analyst.golden.models import Trio, load_trios

log = logging.getLogger(__name__)

_TABLE = "trios"
_DIM = 768  # gemini-embedding-001 supports truncation; 768 is plenty for a few hundred questions


@dataclass
class Match:
    trio: Trio
    score: float          # 0..1, higher is better



class EmbeddingUnavailable(RuntimeError):
    pass


class Embedder:
    def __init__(self, api_key: str | None = None, model: str | None = None, cache_path: Path | None = None):
        self.client = genai.Client(api_key=api_key or settings.gemini_api_key)
        self.model = model or settings.embedding_model
        self.cache_path = cache_path or settings.data_dir / "embed_cache.json"
        self._cache: dict[str, list[float]] = {}
        if self.cache_path.exists():
            try:
                self._cache = json.loads(self.cache_path.read_text())
            except json.JSONDecodeError:
                self._cache = {}

    @staticmethod
    def _key(text: str, task: str) -> str:
        return hashlib.sha1(f"{task}::{text}".encode()).hexdigest()

    def embed(self, texts: list[str], task: str) -> list[list[float]]:
        keys = [self._key(t, task) for t in texts]
        missing = [(i, t) for i, (k, t) in enumerate(zip(keys, texts)) if k not in self._cache]
        if missing:
            try:
                for start in range(0, len(missing), 50):
                    chunk = missing[start : start + 50]
                    res = self.client.models.embed_content(
                        model=self.model,
                        contents=[t for _, t in chunk],
                        config=types.EmbedContentConfig(task_type=task, output_dimensionality=_DIM),
                    )
                    for (i, _), e in zip(chunk, res.embeddings):
                        self._cache[keys[i]] = list(e.values)
            except Exception as e:  # noqa: BLE001 - APIError, httpx transport errors, timeouts: all mean "no embeddings now"
                raise EmbeddingUnavailable(f"{type(e).__name__}: {str(e)[:200]}") from e
            try:
                self.cache_path.write_text(json.dumps(self._cache))
            except OSError as e:  # pragma: no cover
                log.warning("could not persist embedding cache: %s", e)
        return [self._cache[k] for k in keys]


def _fingerprint(trios: list[Trio]) -> str:
    h = hashlib.sha256()
    for t in trios:
        h.update(t.id.encode()); h.update(t.embed_text().encode()); h.update(t.sql.encode())
    return h.hexdigest()[:16]


def _words(text: str) -> set[str]:
    return {w for w in "".join(c.lower() if c.isalnum() else " " for c in text).split() if len(w) > 2}


class GoldenIndex:
    def __init__(self, golden_dir: Path | None = None, db_path: Path | None = None, embedder: Embedder | None = None):
        self.golden_dir = golden_dir or settings.golden_dir
        self.db_path = db_path or settings.lancedb_path
        self.embedder = embedder or Embedder()
        self.trios: dict[str, Trio] = {t.id: t for t in load_trios(self.golden_dir)}
        self.db = lancedb.connect(str(self.db_path))
        self.table = None
        self.index_stale = False          # True when the vector table does not reflect self.trios
        self._ensure_index()

    # -- build ------------------------------------------------------------------------------------
    def _meta_path(self) -> Path:
        return self.db_path / "trios.meta.json"

    def _ensure_index(self) -> None:
        fp = _fingerprint(list(self.trios.values()))
        meta = self._meta_path()
        if _TABLE in self.db.table_names() and meta.exists():
            try:
                if json.loads(meta.read_text()).get("fp") == fp:
                    self.table = self.db.open_table(_TABLE)
                    return
            except (json.JSONDecodeError, OSError):
                pass
        try:
            self.rebuild()
        except EmbeddingUnavailable as e:
            # keep serving whatever table exists (possibly stale); routing degrades to keywords otherwise
            log.warning("could not build the golden index (%s); continuing without embeddings", e)
            self.table = self.db.open_table(_TABLE) if _TABLE in self.db.table_names() else None
            self.index_stale = True

    def rebuild(self) -> None:
        trios = list(self.trios.values())
        vectors = self.embedder.embed([t.embed_text() for t in trios], task="RETRIEVAL_DOCUMENT") if trios else []
        schema = pa.schema([
            pa.field("id", pa.string()),
            pa.field("text", pa.string()),
            pa.field("vector", pa.list_(pa.float32(), _DIM)),
        ])
        rows = [{"id": t.id, "text": t.embed_text(), "vector": v} for t, v in zip(trios, vectors)]
        if _TABLE in self.db.table_names():
            self.db.drop_table(_TABLE)
        self.table = self.db.create_table(_TABLE, data=rows or None, schema=schema)
        self._meta_path().write_text(json.dumps({"fp": _fingerprint(trios), "count": len(trios)}))
        self.index_stale = False

    def add(self, trio: Trio) -> None:
        """Add a promoted trio. Embeds first, so a failure leaves both the files and the index untouched."""
        candidate = {**self.trios, trio.id: trio}
        self.embedder.embed([trio.embed_text()], task="RETRIEVAL_DOCUMENT")   # raises EmbeddingUnavailable
        trio.to_yaml(self.golden_dir / "trios" / f"{trio.id}.yaml")
        self.trios = candidate
        self.rebuild()

    # -- search -----------------------------------------------------------------------------------
    def _keyword_matches(self, q_words: set[str], k: int) -> list[Match]:
        matches = []
        for trio in self.trios.values():
            overlap = len(q_words & _words(trio.embed_text())) / max(1, len(q_words))
            matches.append(Match(trio=trio, score=0.5 * overlap))   # never crosses the replay threshold
        matches.sort(key=lambda m: m.score, reverse=True)
        return matches[:k]

    def search(self, question: str, k: int = 5) -> tuple[list[Match], bool]:
        if not self.trios:
            return [], False
        q_words = _words(question)
        if self.table is None or self.index_stale:
            return self._keyword_matches(q_words, k), True
        try:
            qv = self.embedder.embed([question], task="RETRIEVAL_QUERY")[0]
            hits = self.table.search(qv, vector_column_name="vector").metric("cosine").limit(k).to_list()
        except EmbeddingUnavailable as e:
            log.warning("embeddings unavailable, keyword-only matching: %s", e)
            return self._keyword_matches(q_words, k), True
        matches = []
        for h in hits:
            trio = self.trios.get(h["id"])
            if trio is None:
                continue
            sim = 1.0 - float(h["_distance"])
            overlap = len(q_words & _words(trio.embed_text())) / max(1, len(q_words))
            matches.append(Match(trio=trio, score=min(1.0, 0.9 * sim + 0.1 * overlap)))
        matches.sort(key=lambda m: m.score, reverse=True)
        return matches[:k], False
