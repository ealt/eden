"""Worker-branch uniqueness — chapter 03 §3.3."""

from __future__ import annotations

import pytest
from conformance.harness import _seed
from conformance.harness.wire_client import WireClient

pytestmark = pytest.mark.conformance

CONFORMANCE_GROUP = 'Executor submission'


def test_colliding_variant_id_rejected_so_work_branches_remain_unique(
    wire_client: WireClient,
) -> None:
    """spec/v0/03-roles.md §3.3 — two variants MUST NOT share a worker branch name.

    Construction per MANUAL_UI §26: drive two execute submissions
    against ideas that share a slug, with deliberately-equal
    `variant_id` choices that — under any branch-naming scheme
    derived from `<slug>-<variant_id>` — would produce the same
    `refs/heads/work/<slug>-<variant_id>` ref. The first
    `create_variant` MUST succeed; the second MUST be rejected
    (the reference impl rejects via `eden://error/already-exists`
    on the variant_id collision, which transitively prevents the
    branch collision). The recorded variants observed via
    list_variants MUST end up with no two entries sharing a `branch`
    field — the §3.3 invariant on protocol-owned data.
    """
    pid_a = _seed.create_idea(wire_client, slug="collide")
    pid_b = _seed.create_idea(wire_client, slug="collide")
    shared_branch = "work/collide-shared-id-12345"
    shared_variant_id = "tr-collision-12345"

    v1 = _seed.create_variant(
        wire_client,
        idea_id=pid_a,
        variant_id=shared_variant_id,
        branch=shared_branch,
        status="starting",
    )
    assert v1 == shared_variant_id

    # Second create_variant with the same variant_id (and the same
    # would-be branch) MUST be rejected. The reference impl raises
    # AlreadyExists, surfacing as 409 eden://error/already-exists.
    body = {
        "variant_id": shared_variant_id,
        "experiment_id": wire_client.experiment_id,
        "idea_id": pid_b,
        "status": "starting",
        "parent_commits": ["0" * 40],
        "branch": shared_branch,
        "started_at": "2026-05-01T00:00:00Z",
    }
    r = wire_client.post(wire_client.variants_path(), json=body)
    assert r.status_code == 409, r.text
    assert r.json().get("type") == "eden://error/already-exists"

    # End-state assertion on the §3.3 data invariant: no two variants
    # in the store share a `branch`. The second create was rejected,
    # so list_variants returns exactly one variant carrying
    # `shared_branch`.
    listed = wire_client.get(wire_client.variants_path())
    listed.raise_for_status()
    on_branch = [v for v in listed.json() if v.get("branch") == shared_branch]
    assert len(on_branch) == 1, (
        f"spec/v0/03-roles.md §3.3 forbids two variants sharing a worker "
        f"branch name; observed {len(on_branch)} variants with "
        f"branch={shared_branch!r}: {on_branch}"
    )
    assert on_branch[0]["variant_id"] == shared_variant_id
