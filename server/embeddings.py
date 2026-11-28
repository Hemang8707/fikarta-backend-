"""Dense vectors for Fikarta AI's per-user retrieval (the semantic half of hybrid search).

- What is embedded: only the user's document chunks built by user_rag.build_docs, which never contain a name,
  PAN, phone, email, address or account number; every text also passes user_rag.scrub() first.
- When: only for users who turned on AI chat (the chat endpoint is consent-gated), and only for chunks whose
  content changed (keyed by content hash), so a report is embedded once.
- Where vectors live: next to the chunk, in the same row (user_docs.vec), so the user_id filter that isolates
  documents isolates vectors too. Erasure and account deletion remove them with the rows.
- Failure: any error returns None and retrieval falls back to keyword search (BM25). Never blocks a reply.
"""
import array
import logging
import math
import os
from typing import List, Optional

import httpx

log = logging.getLogger("fikarta.embed")

MODEL = os.getenv("EMBED_MODEL", "gemini-embedding-001")
DIM = int(os.getenv("EMBED_DIM", "768"))
_URL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:batchEmbedContents"
_BATCH = 50


def available() -> bool:
    return bool(os.getenv("GEMINI_API_KEY")) and os.getenv("FIKARTA_AI_DISABLED") != "1" and os.getenv("EMBED_DISABLED") != "1"


def model_id() -> str:
    return f"{MODEL}:{DIM}"


def _norm(v: List[float]) -> List[float]:
    n = math.sqrt(sum(x * x for x in v)) or 1.0
    return [x / n for x in v]


def _call(texts: List[str], task: str) -> Optional[List[List[float]]]:
    import user_rag
    out: List[List[float]] = []
    for i in range(0, len(texts), _BATCH):
        part = [user_rag.scrub(t)[:6000] for t in texts[i:i + _BATCH]]
        body = {"requests": [{"model": f"models/{MODEL}", "content": {"parts": [{"text": t}]}, "taskType": task,
                              "outputDimensionality": DIM} for t in part]}
        try:
            r = httpx.post(_URL.format(model=MODEL), headers={"x-goog-api-key": os.getenv("GEMINI_API_KEY", ""), "Content-Type": "application/json"},
                           json=body, timeout=20)
        except httpx.HTTPError as e:
            log.warning("embed transport error %s", type(e).__name__)
            return None
        if r.status_code != 200:
            log.warning("embed status=%s", r.status_code)
            return None
        try:
            vecs = [e["values"] for e in r.json()["embeddings"]]
        except (KeyError, ValueError, TypeError):
            return None
        if len(vecs) != len(part):
            return None
        out += [_norm(v) for v in vecs]
    return out


def embed_docs(texts: List[str]) -> Optional[List[List[float]]]:
    if not texts or not available():
        return None
    return _call(texts, "RETRIEVAL_DOCUMENT")


def embed_query(text: str) -> Optional[List[float]]:
    if not text or not available():
        return None
    v = _call([text], "RETRIEVAL_QUERY")
    return v[0] if v else None


def pack(v: List[float]) -> bytes:
    return array.array("f", v).tobytes()


def unpack(b: Optional[bytes]) -> Optional[List[float]]:
    if not b:
        return None
    a = array.array("f")
    a.frombytes(b)
    return list(a)


def cosine(a: List[float], b: List[float]) -> float:
    return sum(x * y for x, y in zip(a, b))  # both are unit length
