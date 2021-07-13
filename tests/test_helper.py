import os
import re

import pytest

from user_sync.helper import JobStats, CSVAdapter
from tests.conftest import log_stream


@pytest.fixture()
def field_names():
    return ['firstname', 'lastname', 'email', 'country', 'groups', 'type', 'username', 'domain','untracked', 'extraattribute']


@pytest.fixture()
def adapter():
    return CSVAdapter()


def test_open_csv_file(adapter, tmpdir):
    filename = os.path.join(str(tmpdir), 'test.csv')
    open(filename, 'w').close()
    assert adapter.open_csv_file(filename, 'r')
    assert adapter.open_csv_file(filename, 'w')

    with pytest.raises(ValueError):
        adapter.open_csv_file(filename, 'invalid')


def test_guess_delimiter_from_filename(adapter):
    assert adapter.guess_delimiter_from_filename('helper_test.csv') == ','
    assert adapter.guess_delimiter_from_filename('test.tsv') == '\t'
    assert adapter.guess_delimiter_from_filename('test.mtv') == '\t'


def test_read_csv_rows(adapter, field_names, fixture_dir):
    formatted_result = [{'firstname': 'Dark', 'lastname': 'Wizard', 'email': 'dwizardexample.com', 'country': 'US', 'groups': 'Daleks_Info', 'type': 'federatedID', 'username': None, 'domain': None, 'untracked': None, 'extraattribute': None},
                        {'firstname': '', 'lastname': 'Yennant', 'email': 'dYennant@example.com', 'country': 'US', 'groups': 'Daleks_Info', 'type': 'federatedID', 'username': None, 'domain': None, 'untracked': None, 'extraattribute': None},
                        {'firstname': 'Debra', 'lastname': '', 'email': 'debmorgan@example.com', 'country': 'US', 'groups': 'Daleks_Info', 'type': 'federatedID', 'username': None, 'domain': None, 'untracked': None, 'extraattribute': None},
                        {'firstname': 'Park', 'lastname': 'Wizard', 'email': 'pwizard@example.com', 'country': 'US', 'groups': 'Daleks_Info', 'type': 'federatedID', 'username': 'pwizard@example.com', 'domain': 'example.com', 'untracked': 'Adobe', 'extraattribute': None},
                        {'firstname': 'Shark', 'lastname': 'Wizard', 'email': 'swizard@example.com', 'country': 'US', 'groups': 'Daleks_Info', 'type': 'federatedID', 'username': 'swizard@example.com', 'domain': 'example.com', 'untracked': 'untracked', 'extraattribute': 'Adobe'},
                        {'firstname': 'John', 'lastname': 'Smith', 'email': 'jsmith@example.com', 'country': 'US', 'groups': 'AdobeCC-All', 'type': 'enterpriseID', 'username': '', 'domain': '', 'untracked': None, 'extraattribute': None},
                        {'firstname': 'Jane', 'lastname': 'Doe', 'email': 'jdoe@example.com', 'country': 'US', 'groups': 'AdobeCC-All', 'type': 'federatedID', 'username': '', 'domain': '', 'untracked': None, 'extraattribute': None},
                        {'firstname': 'Richard', 'lastname': 'Roe', 'email': 'rroe@example.com', 'country': 'US', 'groups': 'AdobeCC-All', 'type': '', 'username': '', 'domain': '', 'untracked': None, 'extraattribute': None},
                        {'firstname': 'field1', 'lastname': 'field2', 'email': 'field3', 'country': 'field4', 'groups': 'field5', 'type': 'field6', 'username': 'field7', 'domain': 'field8', 'untracked': 'field9', 'extraattribute': 'field10'},
                        {'firstname': '', 'lastname': '', 'email': '', 'country': '', 'groups': '', 'type': '', 'username': '', 'domain': '', 'untracked': '', 'extraattribute': ''},
                        {'firstname': '', 'lastname': 'Dorathy', 'email': '', 'country': '', 'groups': '', 'type': '', 'username': '', 'domain': '', 'untracked': None, 'extraattribute': None}]

    path = os.path.join(fixture_dir, "test_csv_data.csv")
    csv_yield = list(adapter.read_csv_rows(path, field_names))
    reduced_output = [dict(e) for e in csv_yield]
    assert reduced_output == formatted_result


def test_write_csv_rows(adapter, field_names, tmpdir):
    formatted_result = [{'firstname': 'Dark', 'lastname': 'Wizard', 'email': 'dwizardexample.com', 'country': 'US', 'groups': 'Daleks_Info', 'type': 'federatedID', 'username': '', 'domain': '', 'untracked': '', 'extraattribute': ''},
                        {'firstname': '', 'lastname': 'Yennant', 'email': 'dYennant@example.com', 'country': 'US', 'groups': 'Daleks_Info', 'type': 'federatedID', 'username': '', 'domain': '', 'untracked': '', 'extraattribute': ''},
                        {'firstname': 'Debra', 'lastname': '', 'email': 'debmorgan@example.com', 'country': 'US', 'groups': 'Daleks_Info', 'type': 'federatedID', 'username': '', 'domain': '', 'untracked': '', 'extraattribute': ''},
                        {'firstname': 'Park', 'lastname': 'Wizard', 'email': 'pwizard@example.com', 'country': 'US', 'groups': 'Daleks_Info', 'type': 'federatedID', 'username': 'pwizard@example.com', 'domain': 'example.com', 'untracked': 'Adobe', 'extraattribute': ''},
                        {'firstname': 'Shark', 'lastname': 'Wizard', 'email': 'swizard@example.com', 'country': 'US', 'groups': 'Daleks_Info', 'type': 'federatedID', 'username': 'swizard@example.com', 'domain': 'example.com', 'untracked': 'untracked', 'extraattribute': 'Adobe'},
                        {'firstname': 'John', 'lastname': 'Smith', 'email': 'jsmith@example.com', 'country': 'US', 'groups': 'AdobeCC-All', 'type': 'enterpriseID', 'username': '', 'domain': '', 'untracked': '', 'extraattribute': ''},
                        {'firstname': 'Jane', 'lastname': 'Doe', 'email': 'jdoe@example.com', 'country': 'US', 'groups': 'AdobeCC-All', 'type': 'federatedID', 'username': '', 'domain': '', 'untracked': '', 'extraattribute': ''},
                        {'firstname': 'Richard', 'lastname': 'Roe', 'email': 'rroe@example.com', 'country': 'US', 'groups': 'AdobeCC-All', 'type': '', 'username': '', 'domain': '', 'untracked': '', 'extraattribute': ''},
                        {'firstname': 'field1', 'lastname': 'field2', 'email': 'field3', 'country': 'field4', 'groups': 'field5', 'type': 'field6', 'username': 'field7', 'domain': 'field8', 'untracked': 'field9', 'extraattribute': 'field10'},
                        {'firstname': '', 'lastname': '', 'email': '', 'country': '', 'groups': '', 'type': '', 'username': '', 'domain': '', 'untracked': '', 'extraattribute': ''},
                        {'firstname': '', 'lastname': 'Dorathy', 'email': '', 'country': '', 'groups': '', 'type': '', 'username': '', 'domain': '', 'untracked': '', 'extraattribute': ''}]

    filename = os.path.join(str(tmpdir), 'test.csv')
    field_names.extend(['untracked', 'extraattribute'])
    adapter.write_csv_rows(filename, field_names, formatted_result)

    csv_yield = list(adapter.read_csv_rows(filename, field_names))
    reduced_output = [dict(e) for e in csv_yield]
    assert reduced_output == formatted_result


def test_extra_fields(adapter, tmpdir):
    filename = os.path.join(str(tmpdir), 'test.csv')
    fields = ["field1", "field2"]

    file = open(filename, 'w')
    file.write(",".join(fields) + "\n")
    file.write("val1,val2,val3,val4")
    file.close()

    csv_yield = list(adapter.read_csv_rows(filename, fields))
    assert csv_yield == [{'field2': 'val2', 'field1': 'val1'}]


def test_log_start(log_stream):
    stream, logger = log_stream
    jobstats = JobStats('Test Job Stats')
    jobstats.log_start(logger)
    stream.flush()
    assert stream.getvalue() == '---------- Start Test Job Stats --------------------------------------\n'


def test_log_end(log_stream):
    stream, logger = log_stream
    jobstats = JobStats('Test Job Stats')
    jobstats.log_end(logger)
    stream.flush()
    output = stream.getvalue()
    pattern = '(---------- End Test Job Stats \\(Total time:.*)(\\) ------------------\\n)'
    assert re.search(pattern, output)


def test_create_divider():
    jobstats = JobStats('Test Job Stats')
    line = jobstats.create_divider('This is a header')
    assert line == '----------This is a header--------------------------------------------'
