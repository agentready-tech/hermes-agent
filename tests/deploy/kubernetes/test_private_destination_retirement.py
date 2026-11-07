"""Accidental package cleanup cannot target original releases or a changed destination."""
import copy
from unittest.mock import patch

import pytest

from deploy.kubernetes import retire_private_destination as cleanup


def package():
    return {'id': 16663044, 'name': 'agentsee-hermes-runtime', 'visibility': 'public',
            'version_count': 1, 'repository': {'full_name': cleanup.REPOSITORY}}


def versions():
    return [{'id': 1520165559, 'name': cleanup.DIGEST,
             'metadata': {'container': {'tags': ['2026.11.7.5']}}}]


def test_changed_package_or_version_is_never_deleted():
    cleanup.validate_destination(package(), versions())
    for changed in ({'id': 1}, {'name': 'agentsee-hermes'}, {'visibility': 'private'},
                    {'version_count': 2}, {'repository': {'full_name': 'foreign/repository'}}):
        with pytest.raises(ValueError, match='PACKAGE_SCOPE_CHANGED'):
            cleanup.validate_destination({**package(), **changed}, versions())
    for key, value in [('id', 42), ('name', 'sha256:foreign'),
                       ('metadata', {'container': {'tags': ['2026.11.7.5', 'retained']}})]:
        changed = copy.deepcopy(versions()); changed[0][key] = value
        with pytest.raises(ValueError, match='VERSION_SCOPE_CHANGED'):
            cleanup.validate_destination(package(), changed)


def test_only_reviewed_destination_is_deleted_and_source_is_compared_afterwards():
    with patch.object(cleanup, 'source_snapshot', return_value={'id': 16640069}) as original, \
            patch.object(cleanup, 'api', side_effect=[package(), versions(), None, None]) as api:
        assert cleanup.retire()['originalFiveReleasesPreserved'] is True
    assert original.call_count == 2
    assert api.call_args_list[2].args == (cleanup.RESOURCE,)
    assert api.call_args_list[2].kwargs == {'method': 'DELETE'}
    with patch.object(cleanup, 'source_snapshot', side_effect=[{'id': 1}, {'id': 2}]), \
            patch.object(cleanup, 'api', side_effect=[None, None]), \
            pytest.raises(ValueError, match='RETIREMENT_NOT_CONFIRMED'):
        cleanup.retire()


def test_absent_destination_is_not_deleted_again():
    with patch.object(cleanup, 'source_snapshot', return_value={'id': 16640069}), \
            patch.object(cleanup, 'api', return_value=None) as api:
        assert cleanup.retire()['destinationApi404'] is True
    assert all(call.kwargs.get('method', 'GET') == 'GET' for call in api.call_args_list)
