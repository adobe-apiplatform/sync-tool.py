# Copyright (c) 2016-2017 Adobe Inc.  All rights reserved.
#
# Permission is hereby granted, free of charge, to any person obtaining a copy
# of this software and associated documentation files (the "Software"), to deal
# in the Software without restriction, including without limitation the rights
# to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
# copies of the Software, and to permit persons to whom the Software is
# furnished to do so, subject to the following conditions:
#
# The above copyright notice and this permission notice shall be included in all
# copies or substantial portions of the Software.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
# IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
# FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
# AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
# LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
# OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
# SOFTWARE.

import six
import string

import ldap3

import user_sync.config
import user_sync.connector.helper
import user_sync.error
import user_sync.identity_type
from user_sync.error import AssertionException

import platform
import ssl

def connector_metadata():
    metadata = {
        'name': LDAPDirectoryConnector.name
    }
    return metadata


def connector_initialize(options):
    """
    :type options: dict
    """
    connector = LDAPDirectoryConnector(options)
    return connector


def connector_load_users_and_groups(state, groups=None, extended_attributes=None, all_users=True):
    """
    :type state: LDAPDirectoryConnector
    :type groups: Optional(list(str))
    :type extended_attributes: Optional(list(str))
    :type all_users: bool
    :rtype (bool, iterable(dict))
    """
    return state.load_users_and_groups(groups or [], extended_attributes or [], all_users)


class LDAPDirectoryConnector(object):
    name = 'ldap'

    def __init__(self, caller_options):
        caller_config = user_sync.config.DictConfig('%s configuration' % self.name, caller_options)

        options = self.get_options(caller_config)
        self.options = options

        self.logger = logger = user_sync.connector.helper.create_logger(options)
        logger.debug('%s initialized with options: %s', self.name, options)

        LDAPValueFormatter.encoding = options['string_encoding']
        self.user_identity_type = user_sync.identity_type.parse_identity_type(options['user_identity_type'])
        self.user_identity_type_formatter = LDAPValueFormatter(options['user_identity_type_format'])
        self.user_email_formatter = LDAPValueFormatter(options['user_email_format'])
        self.user_username_formatter = LDAPValueFormatter(options['user_username_format'])
        self.user_domain_formatter = LDAPValueFormatter(options['user_domain_format'])
        self.user_given_name_formatter = LDAPValueFormatter(options['user_given_name_format'])
        self.user_surname_formatter = LDAPValueFormatter(options['user_surname_format'])
        self.user_country_code_formatter = LDAPValueFormatter(options['user_country_code_format'])

        auth_method = options['authentication_method'].lower()
        auth_cred_required = ['simple', 'ntlm']

        if options['username'] is not None:
            if auth_method in auth_cred_required:
                password = caller_config.get_credential('password', options['username'])
            else:
                # Ignore specified credential if authentication method is either 'Kerberos' or 'Anonymous'
                raise AssertionException("'username' and 'password' are not allowed when 'authentication_method' is '%s" % auth_method)
        else:
            # override authentication method to anonymous if username is not specified
            if auth_method != 'anonymous' and auth_method != 'kerberos':
                auth_method = 'anonymous'
                logger.info("Username not specified, overriding authentication method to 'anonymous'")
        # this check must come after we get the password value
        caller_config.report_unused_values(logger)
        if auth_method != 'kerberos':
            from ldap3 import Connection

        if auth_method == 'anonymous':
            auth = {'authentication': ldap3.ANONYMOUS}
            logger.debug('Connecting to: %s - Authentication Method: ANONYMOUS', options['host'])
        elif auth_method == 'simple':
            auth = {'authentication': ldap3.SIMPLE, 'user': six.text_type(options['username']),
                    'password': six.text_type(password)}
            logger.debug('Connecting to: %s - Authentication Method: SIMPLE using username: %s', options['host'],
                         options['username'])
        elif auth_method == 'ntlm':
            auth = {'authentication': ldap3.NTLM, 'user': six.text_type(options['username']),
                    'password': six.text_type(password)}
            logger.debug('Connecting to: %s - Authentication Method: NTLM using username: %s', options['host'],
                         options['username'])
        elif auth_method == 'kerberos':
            if(platform.system() == 'Windows'):
                from .ldap3_extended.Connection import Connection
                auth = {'sasl_credentials': (True,), 'authentication': ldap3.SASL, 'sasl_mechanism': ldap3.GSSAPI}
                logger.debug('Connecting to: %s - Authentication Method: Kerberos', options['host'])
            else:
                raise AssertionException('Kerberos Authentication Method is not supported on this OS. Windows Only')
        else:
            raise AssertionException('LDAP Authentication Method is not supported: %s' % auth_method)

        tls = None
        auto_bind = ldap3.AUTO_BIND_NO_TLS
        if options['require_tls_cert']:
            tls = ldap3.Tls(validate=ssl.CERT_REQUIRED, version=ssl.PROTOCOL_TLSv1_2)
        try:
            server = ldap3.Server(host=options['host'], allowed_referral_hosts=True, tls=tls)
            if server.ssl is False and tls is not None:
                auto_bind = ldap3.AUTO_BIND_TLS_BEFORE_BIND
            connection = Connection(server, auto_bind=auto_bind, read_only=True, **auth)
        except Exception as e:
            raise AssertionException('LDAP connection failure: %s' % e)
        self.connection = connection
        logger.debug('Connected as %s', connection.extend.standard.who_am_i())
        self.user_by_dn = {}
        self.additional_group_filters = None

    @staticmethod
    def get_options(caller_config):
        builder = user_sync.config.OptionsBuilder(caller_config)
        builder.set_string_value('group_filter_format', six.text_type(
            '(&(|(objectCategory=group)(objectClass=groupOfNames)(objectClass=posixGroup))(cn={group}))'))
        builder.set_string_value('all_users_filter', six.text_type(
            '(&(objectClass=user)(objectCategory=person)(!(userAccountControl:1.2.840.113556.1.4.803:=2)))'))
        builder.set_string_value('group_member_filter_format', None)
        builder.set_bool_value('require_tls_cert', False)
        builder.set_dict_value('two_steps_lookup', None)
        builder.set_string_value('string_encoding', 'utf8')
        builder.set_string_value('user_identity_type_format', None)
        builder.set_string_value('user_email_format', six.text_type('{mail}'))
        builder.set_string_value('user_username_format', None)
        builder.set_string_value('user_domain_format', None)
        builder.set_string_value('user_given_name_format', six.text_type('{givenName}'))
        builder.set_string_value('user_surname_format', six.text_type('{sn}'))
        builder.set_string_value('user_country_code_format', six.text_type('{c}'))
        builder.set_string_value('dynamic_group_member_attribute', None)
        builder.set_string_value('user_identity_type', None)
        builder.set_int_value('search_page_size', 200)
        builder.set_string_value('logger_name', LDAPDirectoryConnector.name)
        builder.set_string_value('authentication_method', six.text_type('simple'))
        builder.set_string_value('username', None)
        builder.require_string_value('host')
        builder.require_string_value('base_dn')
        options = builder.get_options()

        options['two_steps_enabled'] = False
        if options['two_steps_lookup'] is not None:
            ts_config = caller_config.get_dict_config('two_steps_lookup', True)
            ts_builder = user_sync.config.OptionsBuilder(ts_config)
            ts_builder.require_string_value('group_member_attribute_name')
            ts_builder.set_bool_value('nested_group', False)
            options['two_steps_enabled'] = True
            options['two_steps_lookup'] = ts_builder.get_options()
            if options['group_member_filter_format']:
                raise AssertionException(
                    "Cannot define both 'group_member_attribute_name' and 'group_member_filter_format' in config")
        else:
            if not options['group_member_filter_format']:
                options['group_member_filter_format'] = six.text_type('(memberOf={group_dn})')
        return options

    def load_users_and_groups(self, groups, extended_attributes, all_users):
        """
        :type groups: list(str)
        :type extended_attributes: list(str)
        :type all_users: bool
        :rtype (bool, iterable(dict))
        """
        options = self.options
        user = {}
        base_dn = six.text_type(options['base_dn'])
        all_users_filter = six.text_type(options['all_users_filter'])
        group_member_filter_format = six.text_type(options['group_member_filter_format'])
        grouped_user_records = {}
        if options['two_steps_enabled']:
            group_member_attribute_name = six.text_type(options['two_steps_lookup']['group_member_attribute_name'])

        # save all the users to memory for faster 2-steps lookup or all_users process
        if all_users:
            try:
                all_users_records = dict(self.iter_users(base_dn, all_users_filter, extended_attributes))
            except Exception as e:
                raise AssertionException('Unexpected LDAP failure reading all users: %s' % e)

        # for each group that's required, do one search for the users of that group
        for group in groups:
            group_users = 0
            group_dn = self.find_ldap_group_dn(group)
            if not group_dn:
                self.logger.warning("No group found for: %s", group)
                continue
            group_member_subfilter = self.format_ldap_query_string(group_member_filter_format, group_dn=group_dn)
            if not group_member_subfilter.startswith('('):
                group_member_subfilter = six.text_type('(') + group_member_subfilter + six.text_type(')')
            user_subfilter = all_users_filter
            if not user_subfilter.startswith('('):
                user_subfilter = six.text_type('(') + user_subfilter + six.text_type(')')
            group_user_filter = six.text_type('(&') + group_member_subfilter + user_subfilter + six.text_type(')')
            group_users = 0
            try:
                if options['two_steps_enabled']:
                    for user_dn in self.iter_group_member_dns(group_dn, group_member_attribute_name):
                        # check to make sure user_dn are within the base_dn scope
                        if self.is_dn_within_base_dn_scope(base_dn, user_dn):
                            # replace base_dn with user_dn and filter with all_users_filter to do user lookup based on DN
                            result = list(self.iter_users(user_dn, all_users_filter, extended_attributes))
                            if result:
                                # iter_users should only return 1 user when doing two_steps lookup.
                                if len(result) > 1:
                                    raise AssertionException(
                                        "Unexpected multiple LDAP object found in 'two_steps_lookup' mode for: %s" % user_dn)
                                else:
                                    user = result[0][1]
                                    user['groups'].append(group)
                                    group_users += 1
                                    grouped_user_records[user_dn] = user
                else:
                    for user_dn, user in self.iter_users(base_dn, group_user_filter, extended_attributes):
                        user['groups'].append(group)
                        group_users += 1
                        grouped_user_records[user_dn] = user
            except Exception as e:
                raise AssertionException('Unexpected LDAP failure reading group members: %s' % e)
            self.logger.debug('Count of users in group "%s": %d', group, group_users)

        # if all users are requested, do an additional search for all of them
        if all_users:
            ungrouped_users = 0
            grouped_users = 0
            try:
                for user_dn, user in self.iter_users(base_dn, all_users_filter, extended_attributes):
                    if not user['groups']:
                        ungrouped_users += 1
                    else:
                        grouped_users += 1
                if groups:
                    self.logger.debug('Count of users in any groups: %d', grouped_users)
                    self.logger.debug('Count of users not in any groups: %d', ungrouped_users)
            except Exception as e:
                raise AssertionException('Unexpected LDAP failure reading all users: %s' % e)

        self.logger.debug('Total users loaded: %d', len(self.user_by_dn))
        return six.itervalues(self.user_by_dn)

    def find_ldap_group_dn(self, group):
        """
        :type group: str
        :rtype str
        """
        connection = self.connection
        options = self.options
        base_dn = six.text_type(options['base_dn'])
        group_filter_format = six.text_type(options['group_filter_format'])
        try:
            filter_string = self.format_ldap_query_string(group_filter_format, group=group)
            connection.search(search_base=base_dn, search_scope=ldap3.SUBTREE, search_filter=filter_string)
            result = connection.entries
        except Exception as e:
            raise AssertionException('Unexpected LDAP failure reading group info: %s' % e)
        group_dn = None
        if len(result) > 0:
            if len(result) > 1:
                raise AssertionException("Multiple LDAP groups found for: %s" % group)
            else:
                if result[0] is not None:
                    group_dn = result[0].entry_dn
        return group_dn

    def iter_group_member_dns(self, group_dn, member_attribute, searched_dns=None):
        """
        return group memberships dns from specified membership attribute in LDAP group object
        :type group: str
        :type member_attribute: str
        :rtype iterable(str)
        """
        if searched_dns is None:
            searched_dns = []
        connection = self.connection
        nested_group_search = self.options['two_steps_lookup']['nested_group']
        try:
            connection.search(search_base=group_dn, search_filter='(objectClass=*)', search_scope=ldap3.SUBTREE,
                              attributes=member_attribute)
            result = connection.entries
            if result is not None:
                record = result[0].entry_attributes_as_dict
                member_dns = LDAPValueFormatter.get_attribute_value(record, member_attribute)
                for member_dn in member_dns or []:
                    # if nested_group search enabled, look up DN and see if group member attribute exist in that object
                    # This will recurse through until there is no nested group.
                    if member_dn not in searched_dns:
                        searched_dns.append(member_dn)
                        if nested_group_search:
                            nested_members = self.iter_group_member_dns(member_dn, member_attribute, searched_dns)
                            for nested_member_dn in nested_members:
                                yield nested_member_dn
                        yield member_dn
        except Exception as e:
            self.logger.warning('Error lookup %s : %s', group_dn, e)
            pass

    def iter_users(self, base_dn, users_filter, extended_attributes):
        options = self.options
        dynamic_group_member_attribute = options['dynamic_group_member_attribute']

        user_attribute_names = []
        user_attribute_names.extend(self.user_given_name_formatter.get_attribute_names())
        user_attribute_names.extend(self.user_surname_formatter.get_attribute_names())
        user_attribute_names.extend(self.user_country_code_formatter.get_attribute_names())
        user_attribute_names.extend(self.user_identity_type_formatter.get_attribute_names())
        user_attribute_names.extend(self.user_email_formatter.get_attribute_names())
        user_attribute_names.extend(self.user_username_formatter.get_attribute_names())
        user_attribute_names.extend(self.user_domain_formatter.get_attribute_names())
        if dynamic_group_member_attribute is not None:
            user_attribute_names.append(six.text_type(dynamic_group_member_attribute))

        extended_attributes = [six.text_type(attr) for attr in extended_attributes]
        extended_attributes = list(set(extended_attributes) - set(user_attribute_names))
        user_attribute_names.extend(extended_attributes)

        result_iter = self.iter_search_result(base_dn, ldap3.SUBTREE, users_filter, user_attribute_names)
        for dn, record in result_iter:
            if dn is None:
                continue
            if dn in self.user_by_dn:
                yield (dn, self.user_by_dn[dn])
                continue

            email, last_attribute_name = self.user_email_formatter.generate_value(record)
            email = email.strip() if email else None
            if not email:
                if last_attribute_name is not None:
                    self.logger.warning('Skipping user with dn %s: empty email attribute (%s)', dn, last_attribute_name)
                continue

            source_attributes = {}

            user = user_sync.connector.helper.create_blank_user()
            source_attributes['email'] = email
            user['email'] = email

            identity_type, last_attribute_name = self.user_identity_type_formatter.generate_value(record)
            if last_attribute_name and not identity_type:
                self.logger.warning('No identity_type attribute (%s) for user with dn: %s, defaulting to %s',
                                    last_attribute_name, dn, self.user_identity_type)
            source_attributes['identity_type'] = identity_type
            if not identity_type:
                user['identity_type'] = self.user_identity_type
            else:
                try:
                    user['identity_type'] = user_sync.identity_type.parse_identity_type(identity_type)
                except AssertionException as e:
                    self.logger.warning('Skipping user with dn %s: %s', dn, e)
                    continue

            username, last_attribute_name = self.user_username_formatter.generate_value(record)
            username = username.strip() if username else None
            source_attributes['username'] = username
            if username:
                user['username'] = username
            else:
                if last_attribute_name:
                    self.logger.warning('No username attribute (%s) for user with dn: %s, default to email (%s)',
                                        last_attribute_name, dn, email)
                user['username'] = email

            domain, last_attribute_name = self.user_domain_formatter.generate_value(record)
            domain = domain.strip() if domain else None
            source_attributes['domain'] = domain
            if domain:
                user['domain'] = domain
            elif username != email:
                user['domain'] = email[email.find('@') + 1:]
            elif last_attribute_name:
                self.logger.warning('No domain attribute (%s) for user with dn: %s', last_attribute_name, dn)

            given_name_value, last_attribute_name = self.user_given_name_formatter.generate_value(record)
            source_attributes['givenName'] = given_name_value
            if given_name_value is not None:
                user['firstname'] = given_name_value
            elif last_attribute_name:
                self.logger.warning('No given name attribute (%s) for user with dn: %s', last_attribute_name, dn)
            sn_value, last_attribute_name = self.user_surname_formatter.generate_value(record)
            source_attributes['sn'] = sn_value
            if sn_value is not None:
                user['lastname'] = sn_value
            elif last_attribute_name:
                self.logger.warning('No surname attribute (%s) for user with dn: %s', last_attribute_name, dn)
            c_value, last_attribute_name = self.user_country_code_formatter.generate_value(record)
            source_attributes['c'] = c_value
            if c_value is not None:
                user['country'] = c_value.upper()

            user['member_groups'] = self.get_member_groups(record, dynamic_group_member_attribute) if self.additional_group_filters else []

            if extended_attributes is not None:
                for extended_attribute in extended_attributes:
                    extended_attribute_value = LDAPValueFormatter.get_attribute_value(record, extended_attribute)
                    source_attributes[extended_attribute] = extended_attribute_value

            user['source_attributes'] = source_attributes.copy()
            if 'groups' not in user:
                user['groups'] = []
            self.user_by_dn[dn] = user

            yield (dn, user)

    def get_member_groups(self, user, dynamic_group_member_attribute):
        """
        Get a list of member group common names for user
        Assumes groups are contained in attribute memberOf
        :param user:
        :return:
        """
        group_names = []
        groups = LDAPValueFormatter.get_attribute_value(user, dynamic_group_member_attribute)

        if not groups:
            return group_names
        elif isinstance(groups, str):
            groups = [groups]

        for group_dn in groups:
            group_cn = self.get_cn_from_dn(group_dn)
            if group_cn:
                group_names.append(group_cn)
        return group_names

    @staticmethod
    def get_cn_from_dn(group_dn):
        """
        Take a DN and return the common name
        Returns None if no common name is found
        If common name is complex (e.g. cn=Bob Jones+email=bob.jones@example.com) then first part of CN is returned
        :param group_dn:
        :return:
        """
        rdn = ldap3.utils.dn.safe_rdn(group_dn)
        if len(rdn) > 0:
            return rdn[0][3:]
        return None

    def iter_search_result(self, base_dn, scope, filter_string, attributes):
        """
        type: filter_string: str
        type: attributes: list(str)
        """
        connection = self.connection
        search_page_size = self.options['search_page_size']
        if search_page_size == 0:
            connection.search(base_dn, filter_string, scope, attributes=attributes)
            entries = connection.entries
            for entry in entries:
                yield [entry.entry_dn, entry.entry_attributes_as_dict]
        else:
            entry_generator = connection.extend.standard.paged_search(search_base=base_dn,
                                                                      search_filter=filter_string,
                                                                      search_scope=scope,
                                                                      attributes=attributes,
                                                                      paged_size=search_page_size,
                                                                      generator=True)
            for entry in entry_generator:
                if entry['type'] != 'searchResRef':
                    yield [entry['dn'], entry['attributes']]

    @staticmethod
    def format_ldap_query_string(query, **kwargs):
        """
        Escape LDAP special characters that may appear in injected query strings
        Should be used with any string that will be injected into an LDAP query.
        :param query:
        :param kwargs:
        :return:
        """
        # See http://www.rfc-editor.org/rfc/rfc4515.txt
        escape_chars = six.text_type('*()\\&|<>~!:')
        escaped_args = {}
        # kwargs is a dict that would normally be passed to string.format
        for k, v in six.iteritems(kwargs):
            # LDAP special characters are escaped in the general format '\' + hex(char)
            # we need to run through the string char by char and if the char exists in
            # the escape_char list, get the ord of it (decimal ascii value), convert it to hex, and
            # replace the '0x' with '\'
            escaped_list = []
            for c in v:
                if c in escape_chars:
                    replace = six.text_type(hex(ord(c))).replace('0x', '\\')
                    escaped_list.append(replace)
                else:
                    escaped_list.append(c)
            escaped_args[k] = six.text_type('').join(escaped_list)
        return query.format(**escaped_args)

    def format_group_user_filter(self, group_dn):
        """
         :type group_dn: str
         :rtype str
         """
        group_member_subfilter = self.format_ldap_query_string(self.options['group_member_filter_format'],
                                                               group_dn=group_dn)
        if not group_member_subfilter.startswith('('):
            group_member_subfilter = six.text_type('(') + group_member_subfilter + six.text_type(')')
        user_subfilter = self.options['all_users_filter']
        if not user_subfilter.startswith('('):
            user_subfilter = six.text_type('(') + user_subfilter + six.text_type(')')
        group_user_filter = six.text_type('(&') + group_member_subfilter + user_subfilter + six.text_type(')')
        return group_user_filter

    @staticmethod
    def is_dn_within_base_dn_scope(base_dn, dn):
        """
        check to see if provided DN is within the base DN scope
        :param base_dn: str
        :param dn: str
        :return: bool
        """
        # return true if base_dn is empty string such as global scope and no need to check user_dn is part of base_dn
        if (not (base_dn and base_dn.strip())):
            return True

        split_base_dn = ldap3.utils.dn.parse_dn(base_dn.lower())
        split_dn = ldap3.utils.dn.parse_dn(dn.lower())
        if split_base_dn == split_dn[-len(split_base_dn):]:
            return True
        return False


class LDAPValueFormatter(object):
    encoding = 'utf8'

    def __init__(self, string_format):
        """
        The format string must be a unicode or ascii string: see notes above about being careful in Py2!
        """
        if string_format is None:
            attribute_names = []
        else:
            string_format = six.text_type(string_format)  # force unicode so attribute values are unicode
            formatter = string.Formatter()
            attribute_names = [six.text_type(item[1]) for item in formatter.parse(string_format) if item[1]]
        self.string_format = string_format
        self.attribute_names = attribute_names

    def get_attribute_names(self):
        """
        :rtype list(str)
        """
        return self.attribute_names

    def generate_value(self, record):
        """
        :type record: dict
        :rtype (unicode, unicode)
        """
        result = None
        attribute_name = None
        if self.string_format is not None:
            values = {}
            for attribute_name in self.attribute_names:
                value = self.get_attribute_value(record, attribute_name, first_only=True)
                if value is None:
                    values = None
                    break
                values[attribute_name] = value
            if values is not None:
                result = self.string_format.format(**values)
        return result, attribute_name

    @classmethod
    def get_attribute_value(cls, attributes, attribute_name, first_only=False):
        """
        The attribute value type must be decodable (str in py2, bytes in py3)
        :type attributes: dict
        :type attribute_name: unicode
        :type first_only: bool
        """
        attribute_values = attributes.get(attribute_name)
        if attribute_values:
            try:
                if isinstance(attribute_values, str):
                    return attribute_values
                else:
                    if first_only:
                        return attribute_values[0]
                    return attribute_values
            except UnicodeError as e:
                raise AssertionException("Encoding error in value of attribute '%s': %s" % (attribute_name, e))
        return None
