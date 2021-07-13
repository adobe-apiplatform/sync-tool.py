import os

import pytest

from user_sync.connector.directory_csv import CSVDirectoryConnector


@pytest.fixture
def default_caller_options():
    return {'email_column_name': 'email',
            'first_name_column_name': 'firstname',
            'last_name_column_name': 'lastname',
            'country_column_name': 'country',
            'groups_column_name': 'groups',
            'identity_type_column_name': 'type',
            'username_column_name': 'username',
            'domain_column_name': 'domain',
            'file_path': None,
            'user_identity_type': 'federatedID'}


@pytest.fixture()
def csv_connector(default_caller_options, fixture_dir):
    default_caller_options['file_path'] = os.path.join(fixture_dir, "test_csv_data.csv")
    return CSVDirectoryConnector(default_caller_options)


@pytest.fixture()
def user_list():
    return {'dYennant@example.com': {'identity_type': 'federatedID', 'username': 'dYennant@example.com', 'domain': None, 'firstname': None, 'lastname': 'Yennant', 'email': 'dYennant@example.com', 'groups': ['Daleks_Info'], 'country': 'US',
                                     'source_attributes': {'email': 'dYennant@example.com', 'firstname': None, 'lastname': 'Yennant', 'country': 'US', 'groups': 'Daleks_Info', 'type': 'federatedID', 'username': None, 'domain': None, 'extraattribute': None}},
            'debmorgan@example.com': {'identity_type': 'federatedID', 'username': 'debmorgan@example.com', 'domain': None, 'firstname': 'Debra', 'lastname': None, 'email': 'debmorgan@example.com', 'groups': ['Daleks_Info'], 'country': 'US',
                                      'source_attributes': {'email': 'debmorgan@example.com', 'firstname': 'Debra', 'lastname': None, 'country': 'US', 'groups': 'Daleks_Info', 'type': 'federatedID', 'username': None, 'domain': None, 'extraattribute': None}},
            'pwizard@example.com': {'identity_type': 'federatedID', 'username': 'pwizard@example.com', 'domain': 'example.com', 'firstname': 'Park', 'lastname': 'Wizard', 'email': 'pwizard@example.com', 'groups': ['Daleks_Info'], 'country': 'US',
                                    'source_attributes': {'email': 'pwizard@example.com', 'firstname': 'Park', 'lastname': 'Wizard', 'country': 'US', 'groups': 'Daleks_Info', 'type': 'federatedID', 'username': 'pwizard@example.com', 'domain': 'example.com', 'extraattribute': None}},
            'swizard@example.com': {'identity_type': 'federatedID', 'username': 'swizard@example.com', 'domain': 'example.com', 'firstname': 'Shark', 'lastname': 'Wizard', 'email': 'swizard@example.com', 'groups': ['Daleks_Info'], 'country': 'US',
                                    'source_attributes': {'email': 'swizard@example.com', 'firstname': 'Shark', 'lastname': 'Wizard', 'country': 'US', 'groups': 'Daleks_Info', 'type': 'federatedID', 'username': 'swizard@example.com', 'domain': 'example.com', 'extraattribute': 'Adobe'}},
            'jsmith@example.com': {'identity_type': 'enterpriseID', 'username': 'jsmith@example.com', 'domain': None, 'firstname': 'John', 'lastname': 'Smith', 'email': 'jsmith@example.com', 'groups': ['AdobeCC-All'], 'country': 'US',
                                   'source_attributes': {'email': 'jsmith@example.com', 'firstname': 'John', 'lastname': 'Smith', 'country': 'US', 'groups': 'AdobeCC-All', 'type': 'enterpriseID', 'username': None, 'domain': None, 'extraattribute': None}},
            'jdoe@example.com': {'identity_type': 'federatedID', 'username': 'jdoe@example.com', 'domain': None, 'firstname': 'Jane', 'lastname': 'Doe', 'email': 'jdoe@example.com', 'groups': ['AdobeCC-All'], 'country': 'US',
                                 'source_attributes': {'email': 'jdoe@example.com', 'firstname': 'Jane', 'lastname': 'Doe', 'country': 'US', 'groups': 'AdobeCC-All', 'type': 'federatedID', 'username': None, 'domain': None, 'extraattribute': None}},
            'rroe@example.com': {'identity_type': 'federatedID', 'username': 'rroe@example.com', 'domain': None, 'firstname': 'Richard', 'lastname': 'Roe', 'email': 'rroe@example.com', 'groups': ['AdobeCC-All'], 'country': 'US',
                                 'source_attributes': {'email': 'rroe@example.com', 'firstname': 'Richard', 'lastname': 'Roe', 'country': 'US', 'groups': 'AdobeCC-All', 'type': None, 'username': None, 'domain': None, 'extraattribute': None}}}


def test_read_users(csv_connector, user_list, fixture_dir):
    returned_users_list = csv_connector.read_users(csv_connector.options['file_path'], ['extraattribute'])
    assert returned_users_list == user_list


def test_get_column_value(csv_connector):
    user = {'firstname': 'Dark', 'lastname': 'Wizard', 'email': 'dwizard@example.com', 'country': None,
            'groups': 'Daleks_Info', 'type': 'federatedID', 'username': None, 'domain': None}

    assert csv_connector.get_column_value(user, 'email') == 'dwizard@example.com'
    assert csv_connector.get_column_value(user, 'country') is None
    assert csv_connector.get_column_value(user, 'country1') is None
