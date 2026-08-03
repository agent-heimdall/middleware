"""Public mutators must refuse the datasets middleware manages on the user's behalf.

Every guard is a pure check on the path, run before the resource is looked up, so none of the
protected paths named here has to exist -- which is what makes covering the whole matrix cheap.
The user-dataset half of the file is the control: it proves the guards reject what they are meant
to and nothing else.
"""

import pytest

from middlewared.test.integration.assets.pool import dataset
from middlewared.test.integration.utils import call, pool

PROTECTED_MESSAGE = "is a protected path."
SNAP = "protection-test"
CLONE_DST = f"{pool}/protection-test-clone"

# One representative of each shape the registry matches: the system dataset, the apps dataset,
# and a dataset in the boot pool. A bare pool root is deliberately absent -- it is not protected.
PROTECTED_DATASETS = [f"{pool}/.system", f"{pool}/ix-apps", "boot-pool/ROOT"]

# `(method, args)` for every guarded public mutator that takes a dataset path.
DATASET_MUTATORS = [
    ("pool.dataset.promote", lambda ds: (ds,)),
    (
        "pool.dataset.rename",
        lambda ds: (ds, {"new_name": f"{pool}/renamed", "force": True}),
    ),
    ("pool.dataset.update", lambda ds: (ds, {})),
    ("pool.dataset.delete", lambda ds: (ds, {"recursive": True})),
    ("pool.dataset.get_quota", lambda ds: (ds, "DATASET")),
    (
        "pool.dataset.set_quota",
        lambda ds: (ds, [{"quota_type": "DATASET", "id": "QUOTA", "quota_value": 0}]),
    ),
    ("pool.dataset.inherit_parent_encryption_properties", lambda ds: (ds,)),
    ("pool.snapshot.create", lambda ds: ({"dataset": ds, "name": SNAP},)),
    ("zfs.resource.destroy", lambda ds: ({"path": ds, "recursive": True},)),
    ("zfs.resource.snapshot.create", lambda ds: ({"dataset": ds, "name": SNAP},)),
]

# The same, for mutators that run as jobs: their guard raises inside the job, so the refusal only
# reaches the caller once the job is waited on.
JOB_DATASET_MUTATORS = [
    ("pool.dataset.lock", lambda ds: (ds, {"force_umount": True})),
    ("pool.dataset.change_key", lambda ds: (ds, {"generate_key": True})),
]

# `(method, args)` for every guarded public mutator that takes a snapshot path.
SNAPSHOT_MUTATORS = [
    ("pool.snapshot.delete", lambda s: (s, {})),
    ("pool.snapshot.rollback", lambda s: (s, {})),
    ("pool.snapshot.hold", lambda s: (s, {})),
    ("pool.snapshot.release", lambda s: (s, {})),
    ("pool.snapshot.rename", lambda s: (s, {"new_name": f"{s}-new", "force": True})),
    ("pool.snapshot.clone", lambda s: ({"snapshot": s, "dataset_dst": CLONE_DST},)),
    ("zfs.resource.snapshot.destroy", lambda s: ({"path": s},)),
    (
        "zfs.resource.snapshot.rename",
        lambda s: ({"current_name": s, "new_name": f"{s}-new"},),
    ),
    ("zfs.resource.snapshot.clone", lambda s: ({"snapshot": s, "dataset": CLONE_DST},)),
    ("zfs.resource.snapshot.hold", lambda s: ({"path": s},)),
    ("zfs.resource.snapshot.release", lambda s: ({"path": s},)),
    ("zfs.resource.snapshot.rollback", lambda s: ({"path": s},)),
]

# Mutators whose *destination* is guarded as well as their source, so that a protected dataset
# cannot be brought into existence by renaming or cloning onto it.
DESTINATION_MUTATORS = [
    (
        "zfs.resource.snapshot.clone",
        lambda src, dst: ({"snapshot": src, "dataset": dst},),
    ),
    ("pool.snapshot.clone", lambda src, dst: ({"snapshot": src, "dataset_dst": dst},)),
]

# The methods whose request models used to carry the `bypass` escape hatch, with payloads that are
# otherwise valid.
BYPASS_PAYLOADS = [
    ("zfs.resource.snapshot.create", {"dataset": f"{pool}/x", "name": "a"}),
    ("zfs.resource.snapshot.destroy", {"path": f"{pool}/x@a"}),
    (
        "zfs.resource.snapshot.rename",
        {"current_name": f"{pool}/x@a", "new_name": f"{pool}/x@b"},
    ),
    ("zfs.resource.snapshot.clone", {"snapshot": f"{pool}/x@a", "dataset": CLONE_DST}),
    ("zfs.resource.snapshot.hold", {"path": f"{pool}/x@a"}),
    ("zfs.resource.snapshot.release", {"path": f"{pool}/x@a"}),
    ("zfs.resource.snapshot.rollback", {"path": f"{pool}/x@a"}),
]

DATASET_IDS = [m for m, _ in DATASET_MUTATORS]
JOB_DATASET_IDS = [m for m, _ in JOB_DATASET_MUTATORS]
SNAPSHOT_IDS = [m for m, _ in SNAPSHOT_MUTATORS]
DESTINATION_IDS = [m for m, _ in DESTINATION_MUTATORS]
BYPASS_IDS = [m for m, _ in BYPASS_PAYLOADS]

# Mutators that would really change a user's dataset, so the control test skips them; they get
# their own round trip further down instead.
DESTRUCTIVE = {"pool.dataset.rename", "pool.dataset.delete", "zfs.resource.destroy"}


def assert_protected(method, args, job=False):
    with pytest.raises(Exception) as exc_info:
        call(method, *args, job=job)

    text = str(exc_info.value)
    assert PROTECTED_MESSAGE in text, text
    assert "[EACCES]" in text, text


def assert_not_protected(method, args, job=False):
    """The call may fail for its own reasons, but never because the path looked protected."""
    try:
        call(method, *args, job=job)
    except Exception as e:
        assert PROTECTED_MESSAGE not in str(e), str(e)


@pytest.mark.parametrize("ds", PROTECTED_DATASETS)
@pytest.mark.parametrize("method,build_args", DATASET_MUTATORS, ids=DATASET_IDS)
def test_dataset_mutator_refuses_protected_dataset(method, build_args, ds):
    assert_protected(method, build_args(ds))


@pytest.mark.parametrize("ds", PROTECTED_DATASETS)
@pytest.mark.parametrize("method,build_args", JOB_DATASET_MUTATORS, ids=JOB_DATASET_IDS)
def test_job_mutator_refuses_protected_dataset(method, build_args, ds):
    assert_protected(method, build_args(ds), job=True)


@pytest.mark.parametrize("ds", PROTECTED_DATASETS)
@pytest.mark.parametrize("method,build_args", SNAPSHOT_MUTATORS, ids=SNAPSHOT_IDS)
def test_snapshot_mutator_refuses_protected_dataset(method, build_args, ds):
    assert_protected(method, build_args(f"{ds}@{SNAP}"))


@pytest.mark.parametrize("ds", PROTECTED_DATASETS)
def test_rename_refuses_a_protected_destination(ds):
    """Renaming *into* a protected path is how one would otherwise be created."""
    with dataset("rename-src") as src:
        assert_protected("pool.dataset.rename", (src, {"new_name": ds, "force": True}))


@pytest.mark.parametrize("ds", PROTECTED_DATASETS)
@pytest.mark.parametrize("method,build_args", DESTINATION_MUTATORS, ids=DESTINATION_IDS)
def test_clone_refuses_a_protected_destination(method, build_args, ds):
    assert_protected(method, build_args(f"{pool}/x@a", ds))


@pytest.mark.parametrize("method,payload", BYPASS_PAYLOADS, ids=BYPASS_IDS)
def test_bypass_is_rejected(method, payload):
    """`bypass` used to let any SNAPSHOT_WRITE caller defeat these guards; it is gone."""
    with pytest.raises(Exception) as exc_info:
        call(method, payload | {"bypass": True})

    text = str(exc_info.value)
    assert "bypass" in text, text
    assert "not permitted" in text.lower(), text


def test_exclude_internal_paths_is_rejected():
    """The listing filter is no longer something a caller can turn off over the wire."""
    with pytest.raises(Exception) as exc_info:
        call("zfs.resource.query", {"exclude_internal_paths": False})

    text = str(exc_info.value)
    assert "exclude_internal_paths" in text, text
    assert "not permitted" in text.lower(), text


def test_pool_root_is_not_protected():
    """A pool root hosting the system dataset stays the user's to manage."""
    assert_not_protected("pool.dataset.promote", (pool,))
    assert_not_protected("zfs.resource.destroy", ({"path": pool},))


def test_user_dataset_is_not_protected():
    """Nothing a user owns is caught by the guards."""
    with dataset("not-protected") as ds:
        for method, build_args in DATASET_MUTATORS:
            if method not in DESTRUCTIVE:
                assert_not_protected(method, build_args(ds))

        for method, build_args in JOB_DATASET_MUTATORS:
            assert_not_protected(method, build_args(ds), job=True)

        # A snapshot that does not exist: every one of these fails with ENOENT, which is exactly
        # what we want -- the call got past the guard without mutating anything.
        for method, build_args in SNAPSHOT_MUTATORS:
            assert_not_protected(method, build_args(f"{ds}@no-such-snapshot"))


def test_user_dataset_can_still_be_renamed():
    with dataset("rename-me") as ds:
        renamed = f"{ds}-renamed"
        call("pool.dataset.rename", ds, {"new_name": renamed, "force": True})
        try:
            assert call("pool.dataset.query", [["id", "=", renamed]])
        finally:
            call("pool.dataset.rename", renamed, {"new_name": ds, "force": True})


def test_user_dataset_can_still_be_destroyed():
    ds = f"{pool}/destroy-me"
    call("pool.dataset.create", {"name": ds})
    call("zfs.resource.destroy", {"path": ds})
    assert not call("pool.dataset.query", [["id", "=", ds]])


def test_internal_datasets_stay_out_of_the_dataset_listing():
    for entry in call("pool.dataset.query"):
        name = entry["id"]
        assert "/.system" not in name, name
        assert "/ix-apps" not in name, name
        assert not name.startswith("boot-pool"), name
