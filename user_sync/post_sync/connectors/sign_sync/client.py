import logging
import json
import time

from user_sync.error import AssertionException

import requests


class SignClient:
    version = 'v5'
    _endpoint_template = 'api/rest/{}/'
    DEFAULT_GROUP_NAME = 'default group'

    def __init__(self, config):
        for k in ['host', 'key', 'admin_email']:
            if k not in config:
                raise AssertionException("Key '{}' must be specified for all Sign orgs".format(k))
        self.host = config['host']
        self.key = config['key']
        self.admin_email = config['admin_email']
        self.console_org = config['console_org'] if 'console_org' in config else None
        self.api_url = None
        self.groups = None
        self.logger = logging.getLogger(self.logger_name())

    def _init(self):
        self.api_url = self.base_uri()
        self.groups = self.get_groups()

    def sign_groups(self):
        if self.api_url is None or self.groups is None:
            self._init()
        return self.groups

    def logger_name(self):
        return 'sign_client.{}'.format(self.console_org if self.console_org else 'main')

    def header(self):
        """
        Return Sign API auth header
        :return: dict()
        """
        if self.version == 'v6':
            return {
                'Authorization': "Bearer {}".format(self.key),
                'Connection': 'close',
            }
        return {
            'Access-Token': self.key,
            'Connection': 'close',
        }

    def header_json(self):
        """
        Get auth headers with options to PUT/POST JSON
        :return: dict()
        """

        json_headers = {
            'Content-Type': 'application/json',
            'Accept': 'application/json',
            'Connection': 'close',
        }
        json_headers.update(self.header())
        return json_headers

    def base_uri(self):
        """
        This function validates that the SIGN integration key is valid.
        :return: dict()
        """

        endpoint = self._endpoint_template.format(self.version)
        url = 'https://' + self.host + '/' + endpoint

        if self.version == 'v6':
            url_path = 'baseUris'
            access_point_key = 'apiAccessPoint'
        else:
            url_path = 'base_uris'
            access_point_key = 'api_access_point'

        result = requests.get(url + url_path, headers=self.header())
        if result.status_code != 200:
            raise AssertionException('Error getting base URI from Sign API, is API key valid?')

        if access_point_key not in result.json():
            raise AssertionException('Error getting base URI for Sign API, result invalid')
        self.logger.debug('base_uri result: {}'.format(result.json()[access_point_key] + endpoint))

        return result.json()[access_point_key] + endpoint

    def call_with_retry(self, method, url, header, data={}):
        """
        Call manager with exponential retry; 3 retries hardcoded beefore quitting
        :return: requests.request object
        """
        retry_nb = 1
        waiting_time = 20
        while retry_nb < 5:
            try:
                waiting_time *= 3
                self.logger.debug('Attempt {} to call: {}'.format(retry_nb, url))
                r = requests.request(method=method, url=url, headers=header, data=data, timeout=120)
                if r.status_code >= 500:
                    raise Exception('Received http code {}, headers: {}'.format(r.status_code, str(r.headers)))
                elif r.status_code == 429:
                    raise Exception('Received http code {} - too many calls. headers: {}'.format(r.status_code, str(r.headers)))
                elif r.status_code > 400 and r.status_code < 500:
                    self.logger.critical(' {} - {}. Headers: {}'.format(r.status_code, r.text, str(r.headers)))
                    raise AssertionException('')
            except Exception as exp:
                self.logger.warning('Failed: {}'.format(exp))
                if retry_nb == 4:
                    raise AssertionException('Quitting after 3 failed retry attempts')
                self.logger.warning('Waiting for {} seconds'.format(waiting_time))
                time.sleep(waiting_time)
                retry_nb +=1
            else:
            	return r

    def get_users(self):
        """
        Get list of all users from Sign (indexed by email address)
        :return: dict()
        """
        if self.api_url is None or self.groups is None:
            self._init()
        users = {}
        header = self.header()
        users_url = self.api_url + 'users'
        self.logger.info('getting list of all Sign users')
        users_res = self.call_with_retry('GET', users_url, header)
 
        for user_id in map(lambda u: u['userId'], users_res.json()['userInfoList']):
            user_url = self.api_url + 'users/' + user_id
            response = self.call_with_retry('GET', user_url, header)
            user = response.json()
            if user['userStatus'] != 'ACTIVE':
               continue
            if user['email'] == self.admin_email:
               continue
            user['userId'] = user_id
            user['roles'] = self.user_roles(user)
            users[user['email']] = user
        return users
 
    def get_groups(self):
        """
        API request to get group information
        :return: dict()
        """
        if self.api_url is None:
            self.api_url = self.base_uri()
        url = self.api_url + 'groups'
        header=self.header()
        res = self.call_with_retry('GET', url, header)
        self.logger.info('getting Sign user groups')
        groups = {}
        sign_groups = res.json()
        for group in sign_groups['groupInfoList']:
            groups[group['groupName'].lower()] = group['groupId']
        return groups

    def create_group(self, group):
        """
        Create a new group in Sign
        :param group: str
        :return:
        """
        if self.api_url is None or self.groups is None:
            self._init()
        url = self.api_url + 'groups'
        header = self.header_json()
        data = json.dumps({'groupName': group})
        self.logger.info('Creating Sign group {} '.format(group))
        res = self.call_with_retry('POST', url, header, data)
        self.groups[group] = res.json()['groupId']

    def update_user(self, user_id, data):
        """
        Update Sign user
        :param user_id: str
        :param data: dict()
        :return: dict()
        """
        if self.api_url is None or self.groups is None:
            self._init()
        url = self.api_url + 'users/' + user_id
        header = self.header_json()
        json_data = json.dumps(data)
        self.call_with_retry('PUT', url, header, json_data)

    @staticmethod
    def user_roles(user):
        """
        Resolve user roles
        :return: list[]
        """
        return ['NORMAL_USER'] if 'roles' not in user else user['roles']
