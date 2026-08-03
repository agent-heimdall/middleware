"""What the mutation guard refuses, and what it lets through.

The guard is the only thing standing between a caller and a dataset middleware manages, so the
cases that matter are the ones where it could quietly stop refusing: an access value that arrived
as a string rather than an enum member, and an access value that is neither.
"""

import errno

import pytest

from middlewared.service_exception import ValidationError
from middlewared.utils.zfs.guard import InternalAccess, deny_protected_path, deny_protected_snapshot

SCHEMA = "zfs.resource.snapshot.destroy"

PROTECTED = "tank/.system"
"""A dataset the system dataset plugin owns."""

USER = "tank/data"
"""An ordinary dataset."""


@pytest.mark.parametrize("guard", [deny_protected_path, deny_protected_snapshot])
def test_protected_path_is_refused_by_default(guard):
    """DENY is the default, so a caller that says nothing gets the refusal."""
    with pytest.raises(ValidationError) as exc:
        guard(SCHEMA, PROTECTED)

    assert exc.value.attribute == SCHEMA
    assert exc.value.errmsg == "'tank/.system' is a protected path."
    assert exc.value.errno == errno.EACCES


@pytest.mark.parametrize("guard", [deny_protected_path, deny_protected_snapshot])
def test_protected_path_is_refused_when_access_is_denied(guard):
    with pytest.raises(ValidationError):
        guard(SCHEMA, PROTECTED, InternalAccess.DENY)


@pytest.mark.parametrize("guard", [deny_protected_path, deny_protected_snapshot])
def test_owner_may_touch_a_protected_path(guard):
    guard(SCHEMA, PROTECTED, InternalAccess.ALLOW)


@pytest.mark.parametrize("guard", [deny_protected_path, deny_protected_snapshot])
@pytest.mark.parametrize("access", [InternalAccess.DENY, InternalAccess.ALLOW])
def test_user_path_passes_either_way(guard, access):
    guard(SCHEMA, USER, access)


@pytest.mark.parametrize("guard", [deny_protected_path, deny_protected_snapshot])
def test_pool_root_is_not_protected(guard):
    """Users have to be able to destroy a pool that happens to host the system dataset."""
    guard(SCHEMA, "tank", InternalAccess.DENY)


def test_snapshot_guard_decides_on_the_dataset():
    """A snapshot of a protected dataset is refused: the suffix is dropped before asking."""
    with pytest.raises(ValidationError) as exc:
        deny_protected_snapshot(SCHEMA, "tank/.system@snap")

    assert exc.value.errmsg == "'tank/.system@snap' is a protected path."
    assert exc.value.errno == errno.EACCES


def test_path_guard_does_not_drop_a_snapshot_suffix():
    """``zfs.resource.destroy`` has to keep telling the caller to use the snapshot endpoint, so the
    non-stripping guard must let a snapshot name fall through to that message."""
    deny_protected_path(SCHEMA, "tank/.system@snap", InternalAccess.DENY)


def test_snapshot_guard_still_refuses_a_bare_dataset():
    with pytest.raises(ValidationError):
        deny_protected_snapshot(SCHEMA, PROTECTED, InternalAccess.DENY)


@pytest.mark.parametrize("guard", [deny_protected_path, deny_protected_snapshot])
def test_access_survives_the_json_hop(guard):
    """``failover.call_remote`` serialises the enum, so the peer node sees a plain string."""
    guard(SCHEMA, PROTECTED, "ALLOW")

    with pytest.raises(ValidationError):
        guard(SCHEMA, PROTECTED, "DENY")


@pytest.mark.parametrize("guard", [deny_protected_path, deny_protected_snapshot])
@pytest.mark.parametrize("access", [True, False, None, 1, "allow", "yes", ""])
def test_an_access_value_that_is_neither_fails_loudly(guard, access):
    """Not every caller of the guard is type checked, so anything that is not one of the two values
    has to raise rather than be interpreted -- ``True`` in particular, which a caller migrating off
    the old boolean flag would pass."""
    with pytest.raises(ValueError):
        guard(SCHEMA, USER, access)
