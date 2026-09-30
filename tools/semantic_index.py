"""Semantic index over AST chunks (Phase 2 of the context rebuild).

Phase 1 (``tools/ast_chunker.py``) turns a source file into structurally complete
:class:`~tools.ast_chunker.CodeUnit` s. This module makes those units *retrievable*: it
embeds each unit's source with a fast sentence encoder and stores the vector beside the
unit's metadata in a local, embedded LanceDB table. A query is embedded the same way and
matched by cosine similarity, returning the top-k whole units -- so a caller gets back
complete classes/functions instead of a dumped file or a sliced window.

Two pieces are deliberately pluggable:

* the **embedder** (:class:`Embedder`). The production default is
  :class:`MiniLmEmbedder` -- ``all-MiniLM-L6-v2``, 384 dimensions, run locally through
  ``transformers``/``torch`` (already present in this environment). :class:`HashingEmbedder`
  is a deterministic, dependency-free, offline stand-in with the same interface, used by
  the tests so the suite needs no model download and no network.
* the **store**. LanceDB is embedded (a directory on disk, no server), which is what a
  desktop app needs: there is no HTTP service here to host a database in.

Similarity is cosine. LanceDB's cosine *distance* is ``1 - cosine_similarity``, so a
:class:`SearchHit` reports ``score = 1 - distance`` (higher is closer), clamped to
``[-1, 1]``.

The hit is a strict Pydantic model (``extra="forbid"``), like every other JSON boundary
in this backend: a retrieved unit is exactly a Phase-1 ``CodeUnit``, byte range and all.

## Dependencies

The store is ``lancedb`` (0.39.0, which embeds ``pyarrow``). The production embedder
uses ``transformers``/``torch``, both already installed in this environment; the model
weights for ``all-MiniLM-L6-v2`` (~90 MB) are fetched from the Hugging Face Hub on first
use and cached locally, so the first embed pays for the download and later runs do not.
The test suite avoids all of that by running on ``HashingEmbedder``.

## The weights load under a cross-process lock

The cache the weights land in is **machine-wide**, so the load has to be serialised across
*processes* and not merely threads: the swarm boots several workers at once, and each one that
plans a task loads this model. Racing them on one cache directory is how a download is left
half-written, a cache is corrupted, or several processes each pull 90 MB at the same time. The
load is therefore taken under a file lock -- a file being the one primitive every process on the
machine can contend for -- so the first process downloads and loads, and the rest block and then
find the cache already populated. ``filelock`` is used rather than a hand-rolled lock because a
subtly wrong lock is worse than a dependency; it is guaranteed present, being a hard requirement
of ``huggingface_hub``, which ``transformers`` -- already required for this embedder -- pulls in.
"""

from __future__ import annotations

import hashlib
import os
import string
import tempfile
import threading
from abc import ABC, abstractmethod
from typing import Any, Dict, Iterable, List, Optional, Sequence

import numpy as np
import pyarrow as pa
import lancedb
from pydantic import BaseModel, ConfigDict

from tools.ast_chunker import CodeUnit, chunk_file, detect_language

_STRICT = ConfigDict(extra="forbid", strict=False)

_DEFAULT_MODEL = "sentence-transformers/all-MiniLM-L6-v2"
_DEFAULT_TABLE = "code_units"

# The cross-process lock directory. Under the system temp directory because it guards a shared,
# machine-wide resource (the model cache), so every process -- in any checkout -- must find the
# same lock file.
_LOCK_DIR = os.path.join(tempfile.gettempdir(), "aleth-embedder-locks")
# One lock per model, shared by every thread here: a single ``FileLock`` instance is what makes
# same-process callers and sibling processes contend for the same file.
_LOCKS: Dict[str, Any] = {}
_LOCKS_GUARD = threading.Lock()


def _lock_path(model_name: str) -> str:
    """A stable lock path per model, so two different models do not block each other."""
    safe = "".join(ch if (ch.isalnum() or ch in "._-") else "-" for ch in model_name)
    return os.path.join(_LOCK_DIR, f"{safe}.lock")


def _model_lock(model_name: str) -> Any:
    """The cross-process lock guarding one model's download and load.

    Imported lazily so the module stays cheap to import, and cached per model so the process holds
    one lock object per set of weights rather than a new one per load.
    """
    with _LOCKS_GUARD:
        lock = _LOCKS.get(model_name)
        if lock is None:
            from filelock import FileLock

            os.makedirs(_LOCK_DIR, exist_ok=True)
            lock = FileLock(_lock_path(model_name))
            _LOCKS[model_name] = lock
    return lock

# The metadata columns beside the vector. Order is the storage contract.
_METADATA_FIELDS = (
    "filepath",
    "language",
    "kind",
    "name",
    "qualified_name",
    "node_type",
    "byte_start",
    "byte_end",
    "line_start",
    "line_end",
    "col_start",
    "col_end",
    "content",
)


class SearchHit(BaseModel):
    """One retrieval result: a whole Phase-1 unit and its cosine similarity to the query.

    ``score`` is the cosine similarity in ``[-1, 1]`` (``1.0`` is an exact direction
    match); hits come back best-first.
    """

    model_config = _STRICT

    score: float
    unit: CodeUnit


class Embedder(ABC):
    """Turns text into L2-normalised float32 vectors.

    With unit-length vectors, cosine similarity is just the dot product, which is what
    lets the store's cosine distance and this module's ``score`` agree.
    """

    @property
    @abstractmethod
    def dim(self) -> int:
        """The vector length this embedder produces."""

    @abstractmethod
    def encode(self, texts: Sequence[str]) -> np.ndarray:
        """Encode ``texts`` into a ``(len(texts), dim)`` float32 array of unit vectors."""


class MiniLmEmbedder(Embedder):
    """The production embedder: ``all-MiniLM-L6-v2`` run locally.

    Mean-pools the token embeddings under the attention mask (the pooling Sentence
    Transformers uses for this model) and L2-normalises the result. The model and
    tokenizer are loaded lazily and cached on first use, so importing this module costs
    nothing and only a caller that actually embeds pays for the weights.
    """

    def __init__(self, model_name: str = _DEFAULT_MODEL):
        self._model_name = model_name
        self._tokenizer = None
        self._model = None

    @property
    def dim(self) -> int:
        return 384

    def _ensure_loaded(self):
        if self._model is not None:
            return self._tokenizer, self._model
        # The load is serialised across processes: several workers booting together would otherwise
        # race on the shared weights cache -- a half-written download, a corrupted cache, or N
        # simultaneous 90 MB fetches. The first holder downloads and loads; the rest wait here and
        # then find the cache populated.
        with _model_lock(self._model_name):
            # Re-check inside the lock: while this process waited, the process that held the lock
            # may have populated the cache, and a sibling thread in *this* process may have loaded
            # the model already. Without this, every waiter would reload it.
            if self._model is None:
                import torch  # local import: keep module import cheap
                from transformers import AutoModel, AutoTokenizer

                tokenizer = AutoTokenizer.from_pretrained(self._model_name)
                model = AutoModel.from_pretrained(self._model_name)
                model.eval()
                self._torch = torch
                self._tokenizer = tokenizer
                # Set last: it is the flag the double-check above reads.
                self._model = model
        return self._tokenizer, self._model

    def encode(self, texts: Sequence[str]) -> np.ndarray:
        texts = list(texts)
        if not texts:
            return np.zeros((0, self.dim), dtype=np.float32)
        tokenizer, model = self._ensure_loaded()
        torch = self._torch
        batch = tokenizer(texts, padding=True, truncation=True, return_tensors="pt")
        with torch.no_grad():
            output = model(**batch)
        mask = batch["attention_mask"].unsqueeze(-1).float()
        summed = (output.last_hidden_state * mask).sum(dim=1)
        counts = mask.sum(dim=1).clamp(min=1e-9)
        vectors = summed / counts
        vectors = torch.nn.functional.normalize(vectors, p=2, dim=1)
        return vectors.cpu().numpy().astype(np.float32)


class HashingEmbedder(Embedder):
    """A deterministic, offline embedder with the same interface as :class:`MiniLmEmbedder`.

    It hashes word tokens into a fixed-width vector (a hashed bag of words), so units that
    share vocabulary land near each other and retrieval is exercisable without a model or
    a network. It is a test/offline stand-in, not a semantic encoder -- it knows nothing
    about meaning, only about which tokens appear.
    """

    def __init__(self, dim: int = 256):
        if dim <= 0:
            raise ValueError("dim must be positive")
        self._dim = dim
        self._strip = string.punctuation + string.whitespace

    @property
    def dim(self) -> int:
        return self._dim

    def _tokens(self, text: str) -> Iterable[str]:
        # Tokenise without a regular expression: split on whitespace, then trim the
        # punctuation around each token with str.strip.
        for raw in text.lower().replace("_", " ").split():
            token = raw.strip(self._strip)
            if token:
                yield token

    def encode(self, texts: Sequence[str]) -> np.ndarray:
        texts = list(texts)
        vectors = np.zeros((len(texts), self._dim), dtype=np.float32)
        for row, text in enumerate(texts):
            for token in self._tokens(text):
                digest = hashlib.blake2b(token.encode("utf-8"), digest_size=8).digest()
                value = int.from_bytes(digest, "big")
                index = value % self._dim
                sign = 1.0 if (value >> 63) & 1 else -1.0
                vectors[row, index] += sign
        norms = np.linalg.norm(vectors, axis=1, keepdims=True)
        np.divide(vectors, norms, out=vectors, where=norms > 0)
        return vectors


def _quote(value: str) -> str:
    """A single-quoted SQL literal for a LanceDB filter (quotes/backslashes escaped)."""
    return value.replace("\\", "\\\\").replace("'", "''")


class CodeUnitIndex:
    """A local, embedded vector index of :class:`CodeUnit` s.

    ``db_path`` is a directory (LanceDB stores a table per name inside it). ``embedder``
    decides the vectors; ``table_name`` allows more than one index per directory (e.g. one
    per workspace).
    """

    def __init__(
        self,
        db_path: str,
        embedder: Embedder,
        table_name: str = _DEFAULT_TABLE,
    ):
        self._db = lancedb.connect(db_path)
        self._embedder = embedder
        self._table_name = table_name

    # -- schema ------------------------------------------------------------------
    @property
    def dim(self) -> int:
        return self._embedder.dim

    def _schema(self) -> pa.Schema:
        fields = [pa.field("vector", pa.list_(pa.float32(), self.dim))]
        for name in _METADATA_FIELDS:
            if name in {"byte_start", "byte_end", "line_start", "line_end", "col_start", "col_end"}:
                fields.append(pa.field(name, pa.int64()))
            else:
                fields.append(pa.field(name, pa.string()))
        return pa.schema(fields)

    def _open_table(self):
        if self._table_name not in self._db.list_tables().tables:
            return None
        return self._db.open_table(self._table_name)

    # -- writing -----------------------------------------------------------------
    def index_units(self, units: Sequence[CodeUnit]) -> int:
        """(Re)build the table from ``units``, returning how many were stored.

        The write replaces any existing table, so indexing a fresh set of units is a
        rebuild rather than an append -- there is no stale vector left pointing at a unit
        that is no longer in the source.
        """
        units = list(units)
        if units:
            vectors = self._embedder.encode([unit.content for unit in units])
            if vectors.shape != (len(units), self.dim):
                raise ValueError(
                    f"embedder returned {vectors.shape}, expected {(len(units), self.dim)}"
                )
            rows = [self._row(unit, vector) for unit, vector in zip(units, vectors)]
            data = pa.Table.from_pylist(rows, schema=self._schema())
        else:
            data = self._schema().empty_table()
        self._db.create_table(self._table_name, data=data, mode="overwrite")
        return len(units)

    def _row(self, unit: CodeUnit, vector: np.ndarray) -> Dict[str, Any]:
        row: Dict[str, Any] = {"vector": vector.astype(np.float32).tolist()}
        for name in _METADATA_FIELDS:
            row[name] = getattr(unit, name)
        return row

    # -- reading -----------------------------------------------------------------
    def count(self) -> int:
        """How many units the table holds (0 when nothing has been indexed yet)."""
        table = self._open_table()
        return 0 if table is None else table.count_rows()

    def search(
        self,
        query: str,
        k: int = 5,
        language: Optional[str] = None,
        filepath: Optional[str] = None,
    ) -> List[SearchHit]:
        """The ``k`` units whose content is closest to ``query`` by cosine similarity.

        ``language`` and ``filepath`` narrow the search before ranking (a prefilter), so a
        caller can ask "the best match in this file" without retrieving the whole table.
        Returns ``[]`` when nothing is indexed.
        """
        if k <= 0:
            raise ValueError("k must be positive")
        table = self._open_table()
        if table is None:
            return []
        query_vector = self._embedder.encode([query])[0].astype(np.float32).tolist()
        search = table.search(query_vector).distance_type("cosine").limit(k)
        where = self._where(language, filepath)
        if where:
            search = search.where(where)
        hits = []
        for row in search.to_list():
            score = max(-1.0, min(1.0, 1.0 - float(row["_distance"])))
            hits.append(SearchHit(score=score, unit=self._unit_from_row(row)))
        return hits

    def _where(self, language: Optional[str], filepath: Optional[str]) -> str:
        clauses = []
        if language:
            clauses.append(f"language = '{_quote(language)}'")
        if filepath:
            clauses.append(f"filepath = '{_quote(filepath)}'")
        return " AND ".join(clauses)

    def _unit_from_row(self, row: Dict[str, Any]) -> CodeUnit:
        return CodeUnit(**{name: row[name] for name in _METADATA_FIELDS})


def retrieve(index: CodeUnitIndex, query: str, k: int = 5) -> List[SearchHit]:
    """The brief's retrieval entry point: top-k structurally complete units for ``query``."""
    return index.search(query, k=k)


def build_index_from_paths(
    paths: Iterable[str],
    db_path: str,
    embedder: Optional[Embedder] = None,
    table_name: str = _DEFAULT_TABLE,
) -> CodeUnitIndex:
    """Chunk every supported file in ``paths`` and index the resulting units.

    Files whose extension has no grammar are skipped rather than failing the build.
    """
    units: List[CodeUnit] = []
    for path in paths:
        if detect_language(path) is None or not os.path.isfile(path):
            continue
        units.extend(chunk_file(path))
    index = CodeUnitIndex(db_path, embedder or MiniLmEmbedder(), table_name)
    index.index_units(units)
    return index
