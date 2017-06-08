# Copyright (c) 2016-2017 Adobe Systems Incorporated.  All rights reserved.
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

import re
import string

import ldap.controls.libldap

import user_sync.config
import user_sync.connector.helper
import user_sync.error
import user_sync.identity_type
from user_sync.error import AssertionException

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


def connector_load_users_and_groups(state, groups, extended_attributes):
    """
    :type state: LDAPDirectoryConnector
    :type groups: list(str)
    :type extended_attributes: list(str)
    :rtype (bool, iterable(dict))
    """
    return state.load_users_and_groups(groups, extended_attributes)

class DirectoryGroupException(AssertionException):
    pass

class LDAPDirectoryConnector(object):
    name = 'ldap'

    group_member_uid_attribute = "memberUid"
    group_member_attribute = "member"
    expected_result_types = [ldap.RES_SEARCH_RESULT, ldap.RES_SEARCH_ENTRY]

    def __init__(self, caller_options):
        caller_config = user_sync.config.DictConfig('%s configuration' % self.name, caller_options)
        builder = user_sync.config.OptionsBuilder(caller_config)
        builder.set_string_value('group_filter_format', '(&'
                                                        '(|(objectCategory=group)'
                                                        '(objectClass=groupOfNames)'
                                                        '(objectClass=posixGroup))'
                                                        '(cn={group})'
                                                        ')')
        builder.set_string_value('all_users_filter', '(&'
                                                     '(objectClass=user)'
                                                     '(objectCategory=person)'
                                                     '(!(userAccountControl:1.2.840.113556.1.4.803:=2))'
                                                     ')')
        builder.set_string_value('group_uid_filter_format', '(&'
                                                            '(|(objectCategory=group)'
                                                            '(objectClass=groupOfNames)''
                                                            '(objectClass=posixGroup))''
                                                            '(uid={group_uid})'
                                                            ')')
        builder.set_string_value('all_groups_filter', '(|'
                                                      '(objectCategory=group)'
                                                      '(objectClass=groupOfNames)'
                                                      '(objectClass=posixGroup)'
                                                      ')')
        builder.set_bool_value('require_tls_cert', False)
        builder.set_string_value('string_encoding', 'utf-8')
        builder.set_string_value('user_identity_type_format', None)
        builder.set_string_value('user_email_format', '{mail}')
        builder.set_string_value('user_username_format', None)
        builder.set_string_value('user_domain_format', None)
        builder.set_string_value('user_identity_type', None)
        builder.set_int_value('search_page_size', 200)
        builder.set_string_value('logger_name', LDAPDirectoryConnector.name)
        host = builder.require_string_value('host')
        username = builder.require_string_value('username')
        builder.require_string_value('base_dn')
        options = builder.get_options()
        self.options = options
        self.logger = logger = user_sync.connector.helper.create_logger(options)
        logger.debug('%s initialized with options: %s', self.name, options)

        LDAPValueFormatter.encoding = options['string_encoding']
        self.user_identity_type = user_sync.identity_type.parse_identity_type(options['user_identity_type'])
        self.user_identity_type_formatter = LDAPValueFormatter(options['user_identity_type_format'])
        self.user_email_formatter = LDAPValueFormatter(options['user_email_format'])
        self.user_username_formatter = LDAPValueFormatter(options['user_username_format'])
        self.user_domain_formatter = LDAPValueFormatter(options['user_domain_format'])

        password = caller_config.get_credential('password', options['username'])
        # this check must come after we get the password value
        caller_config.report_unused_values(logger)

        logger.debug('Connecting to: %s using username: %s', host, username)
        if not options['require_tls_cert']:
            ldap.set_option(ldap.OPT_X_TLS_REQUIRE_CERT, ldap.OPT_X_TLS_NEVER)
        try:
            connection = ldap.initialize(host)
            connection.protocol_version = ldap.VERSION3
            connection.set_option(ldap.OPT_REFERRALS, 0)
            connection.simple_bind_s(username, password)
        except Exception as e:
            raise AssertionException('LDAP connection failure: ' + repr(e))
        self.connection = connection

        try:
            all_groups_filter = options['all_groups_filter']
            base_dn = options['base_dn']

            groups = connection.search_s(
                base_dn,
                ldap.SCOPE_SUBTREE,
                filterstr=all_groups_filter,
                attrlist=[self.group_member_uid_attribute]
            )
            self.group_dns = set([group_dn for group_dn, group_attr in groups if group_dn])
        except Exception as e:
            raise AssertionException('LDAP group information retrieval failure: ' + repr(e))


        logger.debug('Connected')

    def load_users_and_groups(self, groups, extended_attributes):
        """
        :type groups: list(str)
        :type extended_attributes: list(str)
        :rtype (bool, iterable(dict))
        """
        options = self.options
        all_users_filter = options['all_users_filter']

        self.user_by_dn = user_by_dn = {}
        self.user_by_uid = user_by_uid = {}
        for user_dn, user in self.iter_users(all_users_filter, extended_attributes):
            uid = user.get('uid')
            if uid is not None:
                user_by_uid[uid] = user
            user_by_dn[user_dn] = user

        self.logger.debug('Total users loaded: %d', len(user_by_dn))

        for group in groups:
            total_group_members = 0
            total_group_users = 0
            group_members = self.iter_ldap_group_members(group)
            for group_member_attribute, group_member in group_members:
                total_group_members += 1
                if group_member_attribute == self.group_member_uid_attribute:
                    user = user_by_uid.get(group_member)
                else:
                    user = user_by_dn.get(group_member)
                if user is not None:
                    total_group_users += 1
                    user_groups = user['groups']
                    if group not in user_groups:
                        user_groups.append(group)
            self.logger.debug('Group %s members: %d users: %d', group, total_group_members, total_group_users)

        return user_by_dn.itervalues()

    def find_ldap_group(self, group, attribute_list=None):
        """
        Searches for the specified LDAP group, and returns a tuple containing the group's distinguished name, as well as
        any attributes specified in the attributes list. If the member attribute is specified (or no attributes are
        specified), then all nested members of the group are returned as the member attribute.
        :type group: str
        :type attribute_list: list(str)
        :rtype (str, dict)
        """
        connection = self.connection
        options = self.options
        base_dn = options['base_dn']
        group_filter_format = options['group_filter_format']
        group_cn_reg = re.compile(r'(?:^|,)(?:cn|CN)=([^,]*)')
        group_uid_reg = re.compile(r'(?:^|,)(?:uid|UID)=([^,]*)')
        group_dns = self.group_dns

        res = connection.search_s(
            base_dn,
            ldap.SCOPE_SUBTREE,
            filterstr=group_filter_format.format(group=group),
            attrlist=attribute_list
        )

        group_tuple = None
        for current_tuple in res:
            if current_tuple[0] is not None:
                if group_tuple is not None:
                    raise AssertionException("Multiple LDAP groups found for: %s" % group)
                group_tuple = current_tuple

        if not attribute_list or self.group_member_uid_attribute in attribute_list:
            members = set()

            def find_ldap_nested_users(group_dn):
                '''
                Helper function to recursively build a list of nested members, given the specified group or sub-group's
                distinguished name. The function first loads the group's information by determining the group's most
                significant cn or uid, and searches for the group (this seems to be the most compatible approach across
                AD and OpenLDAP). It then determines which members are groups by comparing the individual member dn's
                to the universal list of group dn's, and either drills down into members identified as groups to further
                build the nested user list, or adds the non-group member as a user.
                :param group: group's distinguished name
                '''
                try:
                    group_filter = None
                    group_matches = group_cn_reg.match(group_dn)
                    if group_matches.group(1):
                        group_filter = group_filter_format.format(group=group_matches.group(1))
                    else:
                        group_matches = group_uid_reg.match(group_dn)
                        if group_matches.group(1):
                            group_filter = group_filter_format.format(group=group_matches.group(1))
                        else:
                            raise DirectoryGroupException('Group name "%s" does not contain a common name or uid.' % (group_dn))

                    group_cn = group_matches.group(1)
                    res = connection.search_s(
                        base_dn,
                        ldap.SCOPE_SUBTREE,
                        filterstr=group_filter_format.format(group=group_cn),
                        attrlist=[self.group_member_uid_attribute, self.group_member_attribute]
                    )

                    group_attr = None
                    group_found = False
                    for filt_group_dn, filt_group_attr in res:
                        if filt_group_dn == group_dn:
                            group_attr = filt_group_attr
                            group_found = True
                            break

                    if not group_found:
                        raise DirectoryGroupException('Directory lookup for group with name "%s" failed' % (group_dn))

                    if self.group_member_attribute in group_attr:
                        for member_dn in group_attr[self.group_member_attribute]:
                            if member_dn in group_dns:
                                find_ldap_nested_users(member_dn)
                            else:
                                members.add(member_dn)

                except DirectoryGroupException as e:
                    self.logger.warning(e.message)

            if group_tuple:
                find_ldap_nested_users(group_tuple[0])
                members = list(members)
                members.sort()
                group_tuple[1][self.group_member_attribute] = members
        
        return group_tuple

    def iter_attribute_values(self, dn, attribute_name, attributes=None):
        """
        :type dn: str
        :type attribute_name: str
        :type attributes: dict(str, list)
        :rtype iterator
        """

        connection = self.connection

        msgid = None
        if attributes is None:
            msgid = connection.search(dn, ldap.SCOPE_BASE, attrlist=[attribute_name])

        while True:
            if msgid is not None:
                result_type, result_response = connection.result(msgid)
                msgid = None
                if result_type in self.expected_result_types and len(result_response) > 0:
                    current_tuple = result_response[0]
                    if current_tuple[0] is not None:
                        attributes = current_tuple[1]
            if attributes is None:
                break

            for current_attribute_name, current_attribute_values in attributes.iteritems():
                current_attribute_name_parts = current_attribute_name.split(';')
                if current_attribute_name_parts[0] == attribute_name:
                    if len(current_attribute_name_parts) > 1:
                        upper_bound = self.get_range_upper_bound(current_attribute_name_parts[1])
                        if upper_bound is not None and upper_bound != '*':
                            next_attribute_name = "%s;range=%s-*" % (attribute_name, str(int(upper_bound) + 1))
                            msgid = connection.search(dn, ldap.SCOPE_BASE, attrlist=[next_attribute_name])
                    for current_attribute_value in current_attribute_values:
                        try:
                            yield current_attribute_value
                        except GeneratorExit:
                            if msgid is not None:
                                connection.abandon(msgid)
                            raise
            attributes = None

    def get_range_upper_bound(self, range_statement):
        result = None
        if range_statement is not None:
            statement_parts = range_statement.split('=')
            if statement_parts[0] == 'range' and len(statement_parts) > 1:
                range_parts = statement_parts[1].split('-')
                if len(range_parts) > 1:
                    result = range_parts[1]
        return result

    def iter_ldap_group_members(self, group):
        """
        :type group: str
        :rtype iterator(str, str)
        """
        attributes = [self.group_member_attribute, self.group_member_uid_attribute]
        group_tuple = self.find_ldap_group(group, attributes)
        if group_tuple is None:
            self.logger.warning("No group found for: %s", group)
        else:
            group_dn, group_attributes = group_tuple
            for attribute in attributes:
                attribute_values = self.iter_attribute_values(group_dn, attribute, group_attributes)
                for attribute_value in attribute_values:
                    yield (attribute, attribute_value)

    def iter_users(self, users_filter, extended_attributes):
        options = self.options
        base_dn = options['base_dn']

        user_attribute_names = ["givenName", "sn", "c", "uid"]
        user_attribute_names.extend(self.user_identity_type_formatter.get_attribute_names())
        user_attribute_names.extend(self.user_email_formatter.get_attribute_names())
        user_attribute_names.extend(self.user_username_formatter.get_attribute_names())
        user_attribute_names.extend(self.user_domain_formatter.get_attribute_names())

        extended_attributes = list(set(extended_attributes) - set(user_attribute_names))
        user_attribute_names.extend(extended_attributes)

        result_iter = self.iter_search_result(base_dn, ldap.SCOPE_SUBTREE, users_filter, user_attribute_names)
        for dn, record in result_iter:
            if dn is None:
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
            elif last_attribute_name:
                self.logger.warning('No domain attribute (%s) for user with dn: %s', last_attribute_name, dn)

            given_name_value = LDAPValueFormatter.get_attribute_value(record, 'givenName')
            source_attributes['givenName'] = given_name_value
            if given_name_value is not None:
                user['firstname'] = given_name_value
            sn_value = LDAPValueFormatter.get_attribute_value(record, 'sn')
            source_attributes['sn'] = sn_value
            if sn_value is not None:
                user['lastname'] = sn_value
            c_value = LDAPValueFormatter.get_attribute_value(record, 'c')
            source_attributes['c'] = c_value
            if c_value is not None:
                user['country'] = c_value

            uid = LDAPValueFormatter.get_attribute_value(record, 'uid')
            source_attributes['uid'] = uid
            if uid is not None:
                user['uid'] = uid

            if extended_attributes is not None:
                for extended_attribute in extended_attributes:
                    extended_attribute_value = LDAPValueFormatter.get_attribute_value(record, extended_attribute)
                    source_attributes[extended_attribute] = extended_attribute_value

            user['source_attributes'] = source_attributes.copy()

            yield (dn, user)

    def iter_search_result(self, base_dn, scope, filter_string, attributes):
        """
        type: filter_string: str
        type: attributes: list(str)
        """
        connection = self.connection
        search_page_size = self.options['search_page_size']

        lc = ldap.controls.libldap.SimplePagedResultsControl(True, size=search_page_size, cookie='')

        msgid = None
        try:
            has_next_page = True
            while has_next_page:
                response_data = None
                result_type = None
                if msgid is not None:
                    result_type, response_data, _rmsgid, serverctrls = connection.result3(msgid)
                    msgid = None
                    pctrls = [c for c in serverctrls
                              if c.controlType == ldap.controls.libldap.SimplePagedResultsControl.controlType]
                    if not pctrls:
                        self.logger.warn('Server ignored RFC 2696 control.')
                        has_next_page = False
                    else:
                        lc.cookie = cookie = pctrls[0].cookie
                        if not cookie:
                            has_next_page = False
                if has_next_page:
                    msgid = connection.search_ext(base_dn, scope,
                                                  filterstr=filter_string, attrlist=attributes, serverctrls=[lc])
                if result_type in self.expected_result_types and (response_data is not None):
                    for item in response_data:
                        yield item
        except GeneratorExit:
            if msgid is not None:
                connection.abandon(msgid)
            raise


class LDAPValueFormatter(object):
    encoding = 'utf-8'

    def __init__(self, string_format):
        """
        :type string_format: unicode
        """
        if string_format is None:
            attribute_names = []
        else:
            formatter = string.Formatter()
            attribute_names = [item[1] for item in formatter.parse(string_format) if item[1]]
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
                value = self.get_attribute_value(record, attribute_name)
                if value is None:
                    values = None
                    break
                values[attribute_name] = value
            if values is not None:
                result = self.string_format.format(**values)
        return result, attribute_name

    @classmethod
    def get_attribute_value(cls, attributes, attribute_name):
        """
        :type attributes: dict
        :type attribute_name: unicode
        """
        if attribute_name in attributes:
            attribute_value = attributes[attribute_name]
            if len(attribute_value) > 0:
                try:
                    return attribute_value[0].decode(cls.encoding)
                except UnicodeError as e:
                    raise AssertionException("Encoding error in value of attribute '%s': %s" % (attribute_name, e))
        return None
