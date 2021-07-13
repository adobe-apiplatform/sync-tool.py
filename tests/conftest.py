import os
import logging

import pytest

from user_sync import config
import shutil

from tests.util import ClearableStringIO, blank_user_full
from user_sync.rules import RuleProcessor


@pytest.fixture
def fixture_dir():
    return os.path.abspath(
        os.path.join(
            os.path.dirname(__file__), 'fixture'))


@pytest.fixture
def cli_args():
    def _cli_args(args_in):
        """
        :param dict args:
        :return dict:
        """

        args_out = {}
        for k in config.ConfigLoader.invocation_defaults:
            args_out[k] = None
        for k, v in args_in.items():
            args_out[k] = v
        return args_out

    return _cli_args


@pytest.fixture
def private_key(fixture_dir, tmpdir):
    shutil.copy(os.path.join(fixture_dir, 'test_private.key'), tmpdir.dirname)
    return os.path.join(tmpdir.dirname, 'test_private.key')


@pytest.fixture
def public_cert(fixture_dir, tmpdir):
    shutil.copy(os.path.join(fixture_dir, 'test_cert.crt'), tmpdir.dirname)
    return os.path.join(tmpdir.dirname, 'test_cert.crt')

@pytest.fixture
def root_config_file(fixture_dir):
    return os.path.join(fixture_dir, 'user-sync-config.yml')


@pytest.fixture
def ldap_config_file(fixture_dir):
    return os.path.join(fixture_dir, 'connector-ldap.yml')


@pytest.fixture
def umapi_config_file(fixture_dir):
    return os.path.join(fixture_dir, 'connector-umapi.yml')

@pytest.fixture
def extension_config_file(fixture_dir):
    return os.path.join(fixture_dir, 'extension-config.yml')

@pytest.fixture
def tmp_config_files(root_config_file, ldap_config_file, umapi_config_file, tmpdir):
    tmpfiles = []
    for fname in [root_config_file, ldap_config_file, umapi_config_file]:
        basename = os.path.split(fname)[-1]
        tmpfile = os.path.join(str(tmpdir), basename)
        shutil.copy(fname, tmpfile)
        tmpfiles.append(tmpfile)
    return tuple(tmpfiles)

@pytest.fixture
def resource_file():
    """
    Create an empty resource file
    :return:
    """
    def _resource_file(dirname, filename):
        filepath = os.path.join(dirname, filename)
        open(filepath, 'a').close()
        return filepath
    return _resource_file

@pytest.fixture
def log_stream():
    stream = ClearableStringIO()
    handler = logging.StreamHandler(stream)
    logger = logging.getLogger('test_logger')
    logger.setLevel(logging.DEBUG)
    logger.addHandler(handler)
    yield stream, logger
    handler.close()

@pytest.fixture()
def get_mock_user():
    def _get_mock_user(
            identifier="user1",
            is_umapi_user=False,
            firstname=None,
            lastname=None,
            groups=None,
            country="US",
            identity_type="federatedID",
            domain="example.com",
            username=None
    ):
        u = blank_user_full(identifier, firstname, lastname, groups,
                            country, identity_type, domain, username)
        if is_umapi_user:
            u.pop('identity_type')
            u.pop('member_groups')
            u.pop('source_attributes')
        else:
            u.pop('adminRoles')
            u.pop('status')
            u.pop('type')
        return u

    return _get_mock_user

@pytest.fixture()
def mock_dir_user(get_mock_user):
    return get_mock_user()

@pytest.fixture()
def mock_umapi_user(get_mock_user):
    return get_mock_user(is_umapi_user=True)


@pytest.fixture()
def get_mock_user_list(get_mock_user):
    def _get_mock_user_list(count=5, start=0, umapi_users=False, groups=[]):
        users = {}
        rp = RuleProcessor({})
        get_key = rp.get_umapi_user_key if umapi_users else rp.get_directory_user_key
        for i in range(start, start + count):
            u = get_mock_user("user" + str(i), umapi_users, groups=groups)
            users[get_key(u)] = u
        return users

    return _get_mock_user_list