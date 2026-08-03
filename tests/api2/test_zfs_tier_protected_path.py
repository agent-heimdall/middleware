"""`zfs.tier` must refuse the datasets middleware manages on the user's behalf.

This lives at the top level of `tests/api2/` rather than alongside the other tier tests in
`tests/api2/zfs_tier/`, whose conftest skips the whole directory unless the system is licensed and
has a SPECIAL vdev with spare disks. The refusals here are pure path checks that run before
`zfs.tier` looks at its own configuration, so they are reachable -- and worth asserting -- on any
system, and none of the paths named below has to exist.
"""

import pytest

from middlewared.test.integration.utils import call, pool

PROTECTED_MESSAGE = "is a protected path."

# One representative of each shape the registry matches: the system dataset, the apps dataset, and
# a dataset in the boot pool.
PROTECTED_DATASETS = [f"{pool}/.system", f"{pool}/ix-apps", "boot-pool/ROOT"]

# A well-formed `dataset_name@job_uuid` needs a uuid half, but no job by this id exists anywhere --
# the refusal happens before the daemon is ever asked about it.
JOB_UUID = "00000000-0000-0000-0000-000000000000"

TIER_MUTATORS = [
    (
        "zfs.tier.dataset_set_tier",
        lambda ds: ({"dataset_name": ds, "tier_type": "PERFORMANCE"},),
    ),
    ("zfs.tier.rewrite_job_create", lambda ds: ({"dataset_name": ds},)),
    (
        "zfs.tier.rewrite_job_recover",
        lambda ds: ({"tier_job_id": f"{ds}@{JOB_UUID}"},),
    ),
]

TIER_IDS = [m for m, _ in TIER_MUTATORS]


def assert_protected(method, args):
    with pytest.raises(Exception) as exc_info:
        call(method, *args)

    text = str(exc_info.value)
    assert PROTECTED_MESSAGE in text, text
    assert "[EACCES]" in text, text


@pytest.mark.parametrize("ds", PROTECTED_DATASETS)
@pytest.mark.parametrize("method,build_args", TIER_MUTATORS, ids=TIER_IDS)
def test_tier_mutator_refuses_protected_dataset(method, build_args, ds):
    assert_protected(method, build_args(ds))


@pytest.mark.parametrize("method,build_args", TIER_MUTATORS, ids=TIER_IDS)
def test_ordinary_dataset_is_not_refused(method, build_args):
    """The control: an unmanaged path gets past the check and fails for its own reasons.

    Paired with the test above this is also what proves the check runs first. On a system with
    tiering switched off -- which is every system the tier suite itself skips on -- these calls
    stop at "ZFS tiering is globally disabled", while a managed dataset is refused before that.
    """
    try:
        call(method, *build_args(f"{pool}/no-such-dataset-for-tiering"))
    except Exception as e:
        assert PROTECTED_MESSAGE not in str(e), str(e)
