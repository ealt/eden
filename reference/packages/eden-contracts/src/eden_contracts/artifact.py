"""ArtifactMetadata — the reference artifact store's per-artifact row.

Mirrors ``spec/v0/schemas/artifact-metadata.schema.json``. Recorded on a
wire-level deposit (``07-wire-protocol.md`` §16; ``08-storage.md`` §5.5).
The bytes themselves live behind the opaque ``artifacts_uri``; this row
carries only what ``fetch_artifact`` needs — the depositing principal
(``created_by``) for the §16.2 ACL plus the byte size and content type
for delivery.

A reference-binding detail: the protocol's artifact-store contract
(``08-storage.md`` §5.1–§5.4) is byte-level and does not mandate this row.
"""

from __future__ import annotations

from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field

from ._common import DateTimeStr

OPAQUE_ID_PATTERN = r"^[0-9a-f]{32}$"
"""Grammar for the server-minted opaque artifact id (32 lowercase hex chars).

Single-segment and path-separator-free, so the id can never carry a
path-traversal payload (``07-wire-protocol.md`` §16; ``08-storage.md`` §5.1).
"""


class ArtifactMetadata(BaseModel):
    """Per-artifact metadata row for the reference artifact store."""

    model_config = ConfigDict(strict=True, extra="allow")

    opaque_id: Annotated[str, Field(pattern=OPAQUE_ID_PATTERN)]
    created_by: Annotated[str, Field(min_length=1)]
    size_bytes: Annotated[int, Field(ge=0)]
    content_type: Annotated[str, Field(min_length=1)]
    created_at: DateTimeStr
