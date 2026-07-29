"""Gateways to Persona's existing graph and embedding mechanisms."""

from __future__ import annotations

import math

from app.application.projection.ports import ProjectionCapabilityUnavailable
from app.domains.projection import (
    EmbeddingProjection,
    GraphProjection,
    GraphTriple,
    ProjectionJob,
    ProjectionKind,
)

_MAX_EMBEDDING_DIMENSIONS = 4096


class ExistingGraphGateway:
    kind = ProjectionKind.GRAPH

    async def project(self, job: ProjectionJob) -> GraphProjection:
        from app.knowledge_graph import extract_projection_triples  # noqa: PLC0415

        try:
            raw = await extract_projection_triples(job.source.text)
        except RuntimeError as exc:
            code = str(exc)
            unavailable = code in {
                "graph_provider_unavailable",
                "graph_structured_output_unavailable",
            }
            raise ProjectionCapabilityUnavailable(
                code if code.startswith("graph_") else "graph_provider_failed",
                unavailable=unavailable,
            ) from exc
        triples = tuple(
            GraphTriple(
                subject=str(item["subject"]),
                relation=str(item["relation"]),
                object=str(item["object"]),
            )
            for item in raw
        )
        return GraphProjection(triples=triples)


class ExistingEmbeddingGateway:
    kind = ProjectionKind.EMBEDDING

    async def project(self, job: ProjectionJob) -> EmbeddingProjection:
        from app.memory_vec import embed, embedding_model_name  # noqa: PLC0415

        vector = await embed(job.source.text, kind="document")
        if vector is None:
            raise ProjectionCapabilityUnavailable(
                "embedding_provider_unavailable",
                unavailable=True,
            )
        if not 1 <= len(vector) <= _MAX_EMBEDDING_DIMENSIONS or not all(
            math.isfinite(float(item)) for item in vector
        ):
            raise ProjectionCapabilityUnavailable("embedding_invalid_vector")
        model = (await embedding_model_name()).strip()[:120] or "configured"
        return EmbeddingProjection(
            vector=tuple(float(item) for item in vector),
            model_name=model,
        )


__all__ = ["ExistingEmbeddingGateway", "ExistingGraphGateway"]
