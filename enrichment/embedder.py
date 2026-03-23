"""Generate embeddings for arbitrary text fields across profiles.

Unlike the original system which hardcoded 5 fields, this handles
whatever text fields are present in the dataset. Different datasets
may have different fields (one has "pitch" + "linkedin", another has
"notes" + "call_transcript" + "linkedin").
"""

from __future__ import annotations

import numpy as np
from pathlib import Path

from .models import Profile


class ProfileEmbedder:
    """Generate and manage embeddings for a set of profiles."""

    def __init__(self, model_name: str = "sentence-transformers/all-mpnet-base-v2"):
        self.model_name = model_name
        self._model = None

    @property
    def model(self):
        if self._model is None:
            from sentence_transformers import SentenceTransformer
            self._model = SentenceTransformer(self.model_name)
        return self._model

    def embed_profiles(
        self,
        profiles: list[Profile],
        fields: list[str] | None = None,
        batch_size: int = 64,
    ) -> dict[str, np.ndarray]:
        """Generate embeddings for all specified text fields.

        Args:
            profiles: List of profiles to embed
            fields: Which text fields to embed. If None, auto-detect
                     from the union of all profiles' searchable fields.
            batch_size: Batch size for the embedding model

        Returns:
            Dict of field_name → numpy array of shape (n_profiles, embedding_dim)
        """
        if not profiles:
            return {}

        # Auto-detect fields if not specified
        if fields is None:
            fields = self._detect_fields(profiles)

        embeddings = {}
        for field in fields:
            texts = []
            for p in profiles:
                text = p.searchable_text_fields().get(field, "")
                texts.append(text if text else "")

            # Embed all texts for this field
            vecs = self.model.encode(
                texts,
                batch_size=batch_size,
                show_progress_bar=len(texts) > 100,
                normalize_embeddings=True,
            )
            embeddings[field] = np.array(vecs, dtype=np.float32)

        return embeddings

    def embed_query(self, query: str) -> np.ndarray:
        """Embed a single query string."""
        vec = self.model.encode([query], normalize_embeddings=True)
        return np.array(vec[0], dtype=np.float32)

    def save_embeddings(self, embeddings: dict[str, np.ndarray], path: Path):
        """Save embeddings to .npz file."""
        path.parent.mkdir(parents=True, exist_ok=True)
        np.savez_compressed(str(path), **embeddings)

    def load_embeddings(self, path: Path) -> dict[str, np.ndarray]:
        """Load embeddings from .npz file."""
        data = np.load(str(path))
        return {k: data[k] for k in data.files}

    def _detect_fields(self, profiles: list[Profile]) -> list[str]:
        """Find all text fields present across profiles."""
        field_set = set()
        for p in profiles:
            field_set.update(p.searchable_text_fields().keys())
        return sorted(field_set)
