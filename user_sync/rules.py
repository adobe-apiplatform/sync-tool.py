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
# IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
# FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
# AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
# LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
# OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
# SOFTWARE.

import logging
import six
from itertools import chain
from collections import defaultdict

import user_sync.connector.umapi
import user_sync.error
import user_sync.identity_type
from user_sync.post_sync.manager import PostSyncData
from user_sync.helper import normalize_string, CSVAdapter, JobStats

GROUP_NAME_DELIMITER = '::'
PRIMARY_UMAPI_NAME = None


class RuleProcessor(object):
    # rule processing option defaults
    # these are in alphabetical order!  Always add new ones that way!
    default_options = {
        'adobe_group_filter': None,
        'after_mapping_hook': None,
        'default_country_code': None,
        'delete_strays': False,
        'directory_group_filter': None,
        'disentitle_strays': False,
        'exclude_groups': [],
        'exclude_identity_types': [],
        'exclude_strays': False,
        'exclude_users': [],
        'extended_attributes': set(),
        'extension_enabled': False,
        'process_groups': False,
        'max_adobe_only_users': 200,
        'new_account_type': user_sync.identity_type.ENTERPRISE_IDENTITY_TYPE,
        'remove_strays': False,
        'strategy': 'sync',
        'stray_list_input_path': None,
        'stray_list_output_path': None,
        'test_mode': False,
        'update_user_info': False,
        'username_filter_regex': None,
    }

    def __init__(self, caller_options):
        """
        :type caller_options:dict
        """
        options = dict(self.default_options)
        options.update(caller_options)
        self.options = options
        self.directory_user_by_user_key = {}
        self.filtered_directory_user_by_user_key = {}
        self.umapi_info_by_name = {}
        self.adobeid_user_by_email = {}
        # counters for action summary log
        self.action_summary = {
            # these are in alphabetical order!  Always add new ones that way!
            'adobe_user_groups_created': 0,
            'directory_users_read': 0,
            'directory_users_selected': 0,
            'excluded_user_count': 0,
            'primary_strays_processed': 0,
            'primary_users_created': 0,
            'primary_users_read': 0,
            'secondary_users_created': 0,
            'unchanged_user_count': 0,
            'updated_user_count': 0,
        }
        self.logger = logger = logging.getLogger('processor')

        # save away the exclude options for use in filtering
        self.exclude_groups = self.normalize_groups(options['exclude_groups'])
        self.exclude_identity_types = options['exclude_identity_types']
        self.exclude_users = options['exclude_users']

        # There's a big difference between how we handle the primary umapi,
        # and how we handle secondary umapis.  We care about all the (non-excluded)
        # users in the primary umapi, but we only care about those users in the
        # secondary umapis that match (non-excluded) users in the primary umapi.
        # That's because all we do in the secondary umapis is group management
        # of primary-umapi users, who are presumed to be in primary-umapi domains.
        # So instead of keeping track of excluded users in the primary umapi,
        # we keep track of included users, so we can match them against users
        # in the secondary umapis (and exclude all that don't match).  We track
        # primary users created and secondary users created so that we can figure
        # out which existing users were created in the secondaries only.  Finally,
        # we keep track of user keys that we have updated in any umapi, so that
        # we can correctly report their count.
        self.primary_user_count = 0
        self.included_user_keys = set()
        self.excluded_user_count = 0
        self.primary_users_created = set()
        self.secondary_users_created = set()
        self.updated_user_keys = set()

        # stray key input path comes in, stray_list_output_path goes out
        self.stray_key_map = {}
        if options['stray_list_input_path']:
            self.read_stray_key_map(options['stray_list_input_path'])
        self.stray_list_output_path = options['stray_list_output_path']

        # determine what processing is needed on strays
        self.will_manage_strays = (options['process_groups'] or options['disentitle_strays'] or
                                   options['remove_strays'] or options['delete_strays'])
        self.exclude_strays = options['exclude_strays']
        self.will_process_strays = ((not self.exclude_strays) and
                                    (options['stray_list_output_path'] or self.will_manage_strays))

        # specifying a push strategy disables a lot of processing
        self.push_umapi = False
        if options['strategy'] == 'push':
            self.push_umapi = True
            self.will_manage_strays = False
            self.will_process_strays = False

        # in/out variables for per-user after-mapping-hook code
        self.after_mapping_hook_scope = {
            # in: attributes retrieved from customer directory system (eg 'c', 'givenName')
            # out: N/A
            'source_attributes': None,
            # in: customer-side directory groups found for user
            # out: N/A
            'source_groups': None,
            # in: user's attributes for UMAPI calls as defined by usual rules (eg 'country', 'firstname')
            # out: user's attributes for UMAPI calls as potentially changed by hook code
            'target_attributes': None,
            # in: adobe groups mapped for user by usual rules
            # out: adobe groups as potentially changed by hook code
            'target_groups': None,
            # make logging available to hook code
            'logger': logger,
            # for exclusive use by hook code; persists across calls
            'hook_storage': None,
        }

        # map of username to email address for users that have an email-type username that
        # differs from the user's email address
        self.email_override = {}  # type: dict[str, str]

        # Data to provide to post-sync connectors
        self.post_sync_data = PostSyncData()

        if logger.isEnabledFor(logging.DEBUG):
            options_to_report = options.copy()
            username_filter_regex = options_to_report['username_filter_regex']
            if username_filter_regex is not None:
                options_to_report['username_filter_regex'] = "%s: %s" % (type(username_filter_regex),
                                                                         username_filter_regex.pattern)
            logger.debug('Initialized with options: %s', options_to_report)

    def run(self, directory_groups, directory_connector, umapi_connectors):
        """
        :type directory_groups: dict(str, list(AdobeGroup)
        :type directory_connector: user_sync.connector.directory.DirectoryConnector
        :type umapi_connectors: UmapiConnectors
        """
        logger = self.logger

        self.prepare_umapi_infos()

        if directory_connector is not None:
            load_directory_stats = JobStats("Load from Directory", divider="-")
            load_directory_stats.log_start(logger)
            self.read_desired_user_groups(directory_groups, directory_connector)
            load_directory_stats.log_end(logger)

        for umapi_info in self.umapi_info_by_name.values():
            self.validate_and_log_additional_groups(umapi_info)

        umapi_stats = JobStats('Push to UMAPI' if self.push_umapi else 'Sync with UMAPI', divider="-")
        umapi_stats.log_start(logger)
        if directory_connector is not None:
            # note: push mode is not supported because if it is, we won't have a list of groups
            # that exist in the console.  we don't want to attempt to create groups that already exist
            if self.options.get('process_groups') and not self.push_umapi and self.options.get('auto_create'):
                self.create_umapi_groups(umapi_connectors)
            self.sync_umapi_users(umapi_connectors)
        if self.will_process_strays:
            self.process_strays(umapi_connectors)
        umapi_connectors.execute_actions()
        umapi_stats.log_end(logger)
        self.log_action_summary(umapi_connectors)

    def validate_and_log_additional_groups(self, umapi_info):
        """
        :param umapi_info: UmapiTargetInfo
        :return:
        """
        umapi_name = umapi_info.get_name()
        for mapped, src_groups in umapi_info.get_additional_group_map().items():
            if len(src_groups) > 1:
                raise user_sync.error.AssertionException(
                    "Additional group resolution conflict: {} map to '{}' on '{}'".format(
                        src_groups, mapped, umapi_name if umapi_name else 'primary org'))
            self.logger.info("Mapped additional group '{}' to '{}' on '{}'".format(
                src_groups[0], mapped, umapi_name if umapi_name else 'primary org'))

    def log_action_summary(self, umapi_connectors):
        """
        log number of affected directory and Adobe users,
        and a summary of network actions sent and that had errors
        :type umapi_connectors: UmapiConnectors
        :return: None
        """
        logger = self.logger
        # find the total number of directory users and selected/filtered users
        self.action_summary['directory_users_read'] = len(self.directory_user_by_user_key)
        self.action_summary['directory_users_selected'] = len(self.filtered_directory_user_by_user_key)
        # find the total number of adobe users and excluded users
        self.action_summary['primary_users_read'] = self.primary_user_count
        self.action_summary['excluded_user_count'] = self.excluded_user_count
        self.action_summary['updated_user_count'] = len(self.updated_user_keys)
        # find out the number of users that have no changes; this depends on whether
        # we actually read the directory or read a key file.  So there are two cases:
        if self.action_summary['primary_users_read'] == 0:
            self.action_summary['unchanged_user_count'] = 0
        else:
            self.action_summary['unchanged_user_count'] = (
                    self.action_summary['primary_users_read'] -
                    self.action_summary['excluded_user_count'] -
                    self.action_summary['updated_user_count'] -
                    self.action_summary['primary_strays_processed']
            )
        # find out the number of users created in the primary and secondary umapis
        self.action_summary['primary_users_created'] = len(self.primary_users_created)
        self.action_summary['secondary_users_created'] = len(self.secondary_users_created)

        # English text description for action summary log.
        # The action summary will be shown the same order as they are defined in this list
        if self.push_umapi:
            action_summary_description = [
                ['directory_users_read', 'Number of directory users read'],
                ['directory_users_selected', 'Number of directory users selected for input'],
                ['primary_users_created', 'Number of directory users pushed to Adobe'],
            ]
            if umapi_connectors.get_secondary_connectors():
                action_summary_description += [
                    ['secondary_users_created', 'Number of Adobe users pushed to secondaries'],
                ]
        else:
            action_summary_description = [
                ['directory_users_read', 'Number of directory users read'],
                ['directory_users_selected', 'Number of directory users selected for input'],
                ['primary_users_read', 'Number of Adobe users read'],
                ['excluded_user_count', 'Number of Adobe users excluded from updates'],
                ['unchanged_user_count', 'Number of non-excluded Adobe users with no changes'],
                ['primary_users_created', 'Number of new Adobe users added'],
                ['updated_user_count', 'Number of matching Adobe users updated'],
                ['adobe_user_groups_created', 'Number of Adobe user-groups created'],
            ]
            if umapi_connectors.get_secondary_connectors():
                action_summary_description += [
                    ['secondary_users_created', 'Number of Adobe users added to secondaries'],
                ]
        if self.will_process_strays:
            if self.options['delete_strays']:
                action = 'deleted'
            elif self.options['remove_strays']:
                action = 'removed'
            elif self.options['disentitle_strays']:
                action = 'removed from all groups'
            else:
                action = 'with groups processed'
            action_summary_description.append(['primary_strays_processed', 'Number of Adobe-only users ' + action])

        # prepare the network summary
        umapi_summary_format = 'Number of%s%s UMAPI actions sent (total, success, error)'
        if umapi_connectors.get_secondary_connectors():
            spacer = ' '
            connectors = [('primary', umapi_connectors.get_primary_connector())]
            connectors.extend(six.iteritems(umapi_connectors.get_secondary_connectors()))
        else:
            spacer = ''
            connectors = [('', umapi_connectors.get_primary_connector())]

        # to line up the stats, we pad them out to the longest stat description length,
        # so first we compute that pad length
        pad = 0
        for action_description in action_summary_description:
            if len(action_description[1]) > pad:
                pad = len(action_description[1])
        for name, _ in connectors:
            umapi_summary_description = umapi_summary_format % (spacer, name)
            if len(umapi_summary_description) > pad:
                pad = len(umapi_summary_description)

        # do the report
        if self.options['test_mode']:
            header = '- Action Summary (TEST MODE) -'
        else:
            header = '------- Action Summary -------'
        logger.info('---------------------------' + header + '---------------------------')
        for action_description in action_summary_description:
            description = action_description[1].rjust(pad, ' ')
            action_count = self.action_summary[action_description[0]]
            logger.info('  %s: %s', description, action_count)
        for name, umapi_connector in connectors:
            sent, errors = umapi_connector.get_action_manager().get_statistics()
            description = (umapi_summary_format % (spacer, name)).rjust(pad, ' ')
            logger.info('  %s: (%s, %s, %s)', description, sent, sent - errors, errors)
        logger.info('------------------------------------------------------------------------------------')

    def is_primary_org(self, umapi_info):
        return umapi_info.get_name() == PRIMARY_UMAPI_NAME

    def will_update_user_info(self, umapi_info):
        return self.options['update_user_info'] and self.is_primary_org(umapi_info)

    def will_process_groups(self):
        return self.options['process_groups']

    def will_exclude_unmapped_users(self):
        return self.options['exclude_unmapped_users']

    def get_umapi_info(self, umapi_name):
        umapi_info = self.umapi_info_by_name.get(umapi_name)
        if umapi_info is None:
            self.umapi_info_by_name[umapi_name] = umapi_info = UmapiTargetInfo(umapi_name)
        return umapi_info

    def prepare_umapi_infos(self):
        """
        Make sure we have prepared organizations for all the mapped groups, including extensions.
        """
        for adobe_group in AdobeGroup.iter_groups():
            umapi_info = self.get_umapi_info(adobe_group.get_umapi_name())
            umapi_info.add_mapped_group(adobe_group.get_group_name())

    def read_desired_user_groups(self, mappings, directory_connector):
        """
        :type mappings: dict(str, list(AdobeGroup))
        :type directory_connector: user_sync.connector.directory.DirectoryConnector
        """
        self.logger.debug('Building work list...')

        options = self.options
        directory_group_filter = options['directory_group_filter']
        if directory_group_filter is not None:
            directory_group_filter = set(directory_group_filter)
        extended_attributes = options.get('extended_attributes')

        directory_user_by_user_key = self.directory_user_by_user_key

        directory_groups = set(six.iterkeys(mappings)) if self.will_process_groups() else set()
        if directory_group_filter is not None:
            directory_groups.update(directory_group_filter)
        directory_users = directory_connector.load_users_and_groups(groups=directory_groups,
                                                                    extended_attributes=extended_attributes,
                                                                    all_users=directory_group_filter is None)

        for directory_user in directory_users:
            user_key = self.get_directory_user_key(directory_user)
            if not user_key:
                self.logger.warning("Ignoring directory user with empty user key: %s", directory_user)
                continue
            directory_user_by_user_key[user_key] = directory_user

            if not self.is_directory_user_in_groups(directory_user, directory_group_filter):
                continue
            if not self.is_selected_user_key(user_key):
                continue

            self.filtered_directory_user_by_user_key[user_key] = directory_user
            self.post_sync_data.update_source_attributes(user_key, directory_user['source_attributes'])
            self.get_umapi_info(PRIMARY_UMAPI_NAME).add_desired_group_for(user_key, None)

            # set up groups in hook scope; the target groups will be used whether or not there's customer hook code
            self.after_mapping_hook_scope['source_groups'] = set()
            self.after_mapping_hook_scope['target_groups'] = set()
            for group in directory_user['groups']:
                self.after_mapping_hook_scope['source_groups'].add(group)  # this is a directory group name
                adobe_groups = mappings.get(group)
                if adobe_groups is not None:
                    for adobe_group in adobe_groups:
                        self.after_mapping_hook_scope['target_groups'].add(adobe_group.get_qualified_name())

            # only if there actually is hook code: set up rest of hook scope, invoke hook, update user attributes
            if options['after_mapping_hook'] is not None:
                self.after_mapping_hook_scope['source_attributes'] = directory_user['source_attributes'].copy()

                target_attributes = dict()
                target_attributes['email'] = directory_user.get('email')
                target_attributes['username'] = directory_user.get('username')
                target_attributes['domain'] = directory_user.get('domain')
                target_attributes['firstname'] = directory_user.get('firstname')
                target_attributes['lastname'] = directory_user.get('lastname')
                target_attributes['country'] = directory_user.get('country')
                self.after_mapping_hook_scope['target_attributes'] = target_attributes

                # invoke the customer's hook code
                self.log_after_mapping_hook_scope(before_call=True)
                exec(options['after_mapping_hook'], self.after_mapping_hook_scope)
                self.log_after_mapping_hook_scope(after_call=True)

                # copy modified attributes back to the user object
                directory_user.update(self.after_mapping_hook_scope['target_attributes'])

            for target_group_qualified_name in self.after_mapping_hook_scope['target_groups']:
                target_group = AdobeGroup.lookup(target_group_qualified_name)
                if target_group is not None:
                    umapi_info = self.get_umapi_info(target_group.get_umapi_name())
                    umapi_info.add_desired_group_for(user_key, target_group.get_group_name())
                else:
                    self.logger.error('Target adobe group %s is not known; ignored', target_group_qualified_name)

            additional_groups = self.options.get('additional_groups', [])
            member_groups = directory_user.get('member_groups', [])
            for member_group in member_groups:
                for group_rule in additional_groups:
                    source = group_rule['source']
                    target = group_rule['target']
                    target_name = target.get_group_name()
                    umapi_info = self.get_umapi_info(target.get_umapi_name())
                    if not group_rule['source'].match(member_group):
                        continue
                    try:
                        rename_group = source.sub(target_name, member_group)
                    except Exception as e:
                        raise user_sync.error.AssertionException("Additional group resolution error: {}".format(str(e)))
                    umapi_info.add_mapped_group(rename_group)
                    umapi_info.add_additional_group(rename_group, member_group)
                    umapi_info.add_desired_group_for(user_key, rename_group)

        self.logger.debug('Total directory users after filtering: %d', len(self.filtered_directory_user_by_user_key))
        if self.logger.isEnabledFor(logging.DEBUG):
            self.logger.debug('Group work list: %s', dict([(umapi_name, umapi_info.get_desired_groups_by_user_key())
                                                           for umapi_name, umapi_info
                                                           in six.iteritems(self.umapi_info_by_name)]))

    def is_directory_user_in_groups(self, directory_user, groups):
        """
        :type directory_user: dict
        :type groups: set
        :rtype bool
        """
        if groups is None:
            return True
        for directory_user_group in directory_user['groups']:
            if directory_user_group in groups:
                return True
        return False

    def sync_umapi_users(self, umapi_connectors):
        """
        This is where we actually "do the sync"; that is, where we match users on the two sides.
        When we get here, we have loaded all the directory users.  Then, for each umapi connector,
        we sync the directory users against the user in the umapi connector, yielding a set of
        unmatched directory users which we then create on the Adobe side.
        :type umapi_connectors: UmapiConnectors
        """
        if self.push_umapi:
            verb = "Push"
        else:
            verb = "Sync"
        exclude_unmapped_users = self.will_exclude_unmapped_users()
        # first sync the primary connector, so the users get created in the primary
        if umapi_connectors.get_secondary_connectors():
            self.logger.debug('%sing users to primary umapi...', verb)
        else:
            self.logger.debug('%sing users to umapi...', verb)
        umapi_info, umapi_connector = self.get_umapi_info(PRIMARY_UMAPI_NAME), umapi_connectors.get_primary_connector()
        if self.push_umapi:
            primary_adds_by_user_key = umapi_info.get_desired_groups_by_user_key()
        else:
            primary_adds_by_user_key = self.update_umapi_users_for_connector(umapi_info, umapi_connector)
        # save groups for new users

        total_users = len(primary_adds_by_user_key)

        user_count = 0
        for user_key, groups_to_add in six.iteritems(primary_adds_by_user_key):
            user_count += 1
            if exclude_unmapped_users and not groups_to_add:
                # If user is not part of any group and ignore outcast is enabled. Do not create user.
                continue
            # We always create every user in the primary umapi, because it's believed to own the directories.
            if user_count % 10 == 0:
                self.logger.progress(user_count, total_users, 'actions completed')
            self.primary_users_created.add(user_key)
            self.create_umapi_user(user_key, groups_to_add, umapi_info, umapi_connector)

        # then sync the secondary connectors
        for umapi_name, umapi_connector in six.iteritems(umapi_connectors.get_secondary_connectors()):
            umapi_info = self.get_umapi_info(umapi_name)
            if len(umapi_info.get_mapped_groups()) == 0:
                continue
            self.logger.debug('%sing users to secondary umapi %s...', verb, umapi_name)
            if self.push_umapi:
                secondary_adds_by_user_key = umapi_info.get_desired_groups_by_user_key()
            else:
                secondary_adds_by_user_key = self.update_umapi_users_for_connector(umapi_info, umapi_connector)
            total_users = len(secondary_adds_by_user_key)
            for user_key, groups_to_add in six.iteritems(secondary_adds_by_user_key):
                # We only create users who have group mappings in the secondary umapi
                if groups_to_add:
                    self.logger.progress(user_count, total_users,
                                         'Adding user to umapi {0} with user key: {1}'.format(umapi_name, user_key))
                    self.secondary_users_created.add(user_key)
                    if user_key not in self.primary_users_created:
                        # We pushed an existing user to a secondary in order to update his groups
                        self.updated_user_keys.add(user_key)
                    self.create_umapi_user(user_key, groups_to_add, umapi_info, umapi_connector)

    def create_umapi_groups(self, umapi_connectors):
        """
        This is where we create user-groups. If auto_create is enabled,
        this will pull user-groups from console and compare with mapped_groups. If mapped group does exist
        in the console, then it will create. Note: Push Mode is not supported
        :type umapi_connectors: UmapiConnectors
        """
        for umapi_connector in umapi_connectors.connectors:
            umapi_name = None if umapi_connector.name.split('.')[-1] == 'primary' \
                else umapi_connector.name.split('.')[-1]
            if umapi_name == 'umapi':
                umapi_name = None
            if umapi_name not in self.umapi_info_by_name:
                continue
            umapi_info = self.umapi_info_by_name[umapi_name]
            mapped_groups = umapi_info.get_non_normalize_mapped_groups()

            # pull all user groups from console
            on_adobe_groups = [normalize_string(g['groupName']) for g in umapi_connector.get_groups()]

            # verify if group exist and create
            for mapped_group in mapped_groups:
                if normalize_string(mapped_group) in on_adobe_groups:
                    continue
                self.logger.info("Auto create user-group enabled: Creating '{}' on '{}'".format(
                    mapped_group, umapi_name if umapi_name else 'primary org'))
                try:
                    # create group
                    res = umapi_connector.create_group(mapped_group)
                    self.action_summary['adobe_user_groups_created'] += 1
                except Exception as e:
                    self.logger.critical("Unable to create %s user group: '{}' on '{}' (error: {})".format(
                        mapped_group, umapi_name if umapi_name else 'primary org', e))

    def is_selected_user_key(self, user_key):
        """
        :type user_key: str
        """
        username_filter_regex = self.options['username_filter_regex']
        if username_filter_regex is not None:
            username = self.get_username_from_user_key(user_key)
            search_result = username_filter_regex.search(username)
            if search_result is None:
                return False
        return True

    def get_stray_keys(self, umapi_name=PRIMARY_UMAPI_NAME):
        return self.stray_key_map.get(umapi_name, {})

    def add_stray(self, umapi_name, user_key, removed_groups=None):
        """
        Remember that this user is a stray found in this umapi connector.  The special marker value None
        means that we are about to start processing this connector, so initialize the map for it.
        :param umapi_name: name of the umapi connector the user was found in
        :param user_key: user_key (str) from a user in that connector
        :param removed_groups: a set of adobe_groups to be removed from the user in that umapi
        """
        if user_key is None:
            if umapi_name not in self.stray_key_map:
                self.stray_key_map[umapi_name] = {}
        else:
            self.stray_key_map[umapi_name][user_key] = removed_groups

    def process_strays(self, umapi_connectors):
        """
        Do the top-level logic for stray processing (output to file or clean them up), enforce limits, etc.
        The actual work is done in sub-functions that we call.
        :param umapi_connectors:
        :return:
        """
        stray_count = len(self.get_stray_keys())
        if self.stray_list_output_path:
            self.write_stray_key_map()
        if self.will_manage_strays:
            max_missing_option = self.options['max_adobe_only_users']
            if isinstance(max_missing_option, str) and '%' in max_missing_option:
                percent = float(max_missing_option.strip('%')) / 100
                max_missing = int((self.primary_user_count - self.excluded_user_count) * percent)
            else:
                max_missing = max_missing_option
            if stray_count > max_missing:
                self.logger.critical('Unable to process Adobe-only users, as their count (%s) is larger '
                                     'than the max_adobe_only_users setting (%s)', stray_count, max_missing_option)
                self.action_summary['primary_strays_processed'] = 0
                return
            self.logger.debug("Processing Adobe-only users...")
            self.manage_strays(umapi_connectors)

    def manage_strays(self, umapi_connectors):
        """
        Manage strays.  This doesn't require having loaded users from the umapi.
        Management of groups, removal of entitlements and removal from umapi are
        processed against every secondary umapi, whereas account deletion is only done
        against the primary umapi.
        Because all directory users are assumed to be in the primary (as the owning org of the directory),
        we don't pay any attention to stray users in the secondary who aren't in the primary.  Instead,
        we assume that they are users whose directory is owned by the secondary.
        :type umapi_connectors: UmapiConnectors
        """
        # figure out what management to do
        manage_stray_groups = self.will_process_groups()
        disentitle_strays = self.options['disentitle_strays']
        remove_strays = self.options['remove_strays']
        delete_strays = self.options['delete_strays']

        # all our processing is controlled by the strays in the primary organization
        primary_strays = self.get_stray_keys()
        self.action_summary['primary_strays_processed'] = total_strays = len(primary_strays)

        # convenience function to get umapi Commands given a user key
        def get_commands(key):
            """Given a user key, returns the umapi commands targeting that user"""
            id_type, username, domain = self.parse_user_key(key)
            if '@' in username and username in self.email_override:
                username = self.email_override[username]
            return user_sync.connector.umapi.Commands(identity_type=id_type, username=username, domain=domain)

        # do the secondary umapis first, in case we are deleting user accounts from the primary umapi at the end
        for umapi_name, umapi_connector in six.iteritems(umapi_connectors.get_secondary_connectors()):
            secondary_strays = self.get_stray_keys(umapi_name)
            for user_key in primary_strays:
                if user_key in secondary_strays:
                    commands = get_commands(user_key)
                    if disentitle_strays:
                        self.logger.info('Removing all adobe groups in %s for Adobe-only user: %s',
                                         umapi_name, user_key)
                        self.post_sync_data.remove_umapi_user_groups(umapi_name, user_key)
                        commands.remove_all_groups()
                    elif remove_strays or delete_strays:
                        self.logger.info('Removing Adobe-only user from %s: %s',
                                         umapi_name, user_key)
                        self.post_sync_data.remove_umapi_user(umapi_name, user_key)
                        commands.remove_from_org(False)
                    elif manage_stray_groups:
                        groups_to_remove = secondary_strays[user_key]
                        if groups_to_remove:
                            self.logger.info('Removing mapped groups in %s from Adobe-only user: %s',
                                             umapi_name, user_key)
                            self.post_sync_data.update_umapi_data(umapi_name, user_key, [], groups_to_remove)
                            commands.remove_groups(groups_to_remove)
                        else:
                            continue
                    else:
                        # haven't done anything, don't send commands
                        continue
                    umapi_connector.send_commands(commands)
            # make sure the commands for each umapi are executed before moving to the next
            umapi_connector.get_action_manager().flush()

        # finish with the primary umapi
        primary_connector = umapi_connectors.get_primary_connector()
        for i, user_key in enumerate(primary_strays):
            per = round(100*(float(i)/float(total_strays)),3)
            commands = get_commands(user_key)
            if disentitle_strays:
                self.logger.info('Removing all adobe groups for Adobe-only user: %s', user_key)
                self.post_sync_data.remove_umapi_user_groups(None, user_key)
                commands.remove_all_groups()
            elif remove_strays or delete_strays:
                action = "Deleting" if delete_strays else "Removing"
                self.logger.info('(%s/%s)(%s%%) %s Adobe-only user: %s', i, total_strays, per, action, user_key)
                self.post_sync_data.remove_umapi_user(None, user_key)
                commands.remove_from_org(True if delete_strays else False)
            elif manage_stray_groups:
                groups_to_remove = primary_strays[user_key]
                if groups_to_remove:
                    self.logger.info('Removing mapped groups from Adobe-only user: %s', user_key)
                    self.post_sync_data.update_umapi_data(None, user_key, [], groups_to_remove)
                    commands.remove_groups(groups_to_remove)
                else:
                    continue
            else:
                # haven't done anything, don't send commands
                continue
            primary_connector.send_commands(commands)
        # make sure the actions get sent
        primary_connector.get_action_manager().flush()

    @staticmethod
    def get_user_attributes(directory_user):
        return {'email': directory_user['email'], 'firstname': directory_user['firstname'],
                'lastname': directory_user['lastname']}

    def get_identity_type_from_directory_user(self, directory_user):
        identity_type = directory_user.get('identity_type')
        if identity_type is None:
            identity_type = self.options['new_account_type']
            self.logger.warning('Found user with no identity type, using %s: %s', identity_type, directory_user)
        return identity_type

    def get_identity_type_from_umapi_user(self, umapi_user):
        identity_type = umapi_user.get('type')
        if identity_type is None:
            identity_type = self.options['new_account_type']
            self.logger.error('Found adobe user with no identity type, using %s: %s', identity_type, umapi_user)
        return identity_type

    def create_umapi_commands_for_directory_user(self, directory_user, do_update=False, console_trusted=False):
        """
        Make the umapi commands to create this user, based on his directory attributes and type.
        Update the attributes of an existing user if do_update is True.
        :type directory_user: dict
        :type do_update: bool
        :return user_sync.connector.umapi.Commands (or None if there's an error)
        """
        identity_type = self.get_identity_type_from_directory_user(directory_user)
        update_username = None

        # check to see if AdobeID exist for FederatedID/EnterpriseID user. Skip user if same email exist.
        if ((identity_type == user_sync.identity_type.FEDERATED_IDENTITY_TYPE or
             identity_type == user_sync.identity_type.ENTERPRISE_IDENTITY_TYPE) and
                self.is_adobeID_email_exist(directory_user['email'])):
            self.logger.warning("Skipping user creation for: %s - AdobeID already exists with %s",
                                self.get_directory_user_key(directory_user), directory_user['email'])
            return None

        if (identity_type == user_sync.identity_type.FEDERATED_IDENTITY_TYPE and directory_user['username'] and
                '@' in directory_user['username'] and
                normalize_string(directory_user['email']) != normalize_string(directory_user['username'])):
            update_username = directory_user['username']
            directory_user['username'] = directory_user['email']

        commands = user_sync.connector.umapi.Commands(identity_type, directory_user['email'],
                                                      directory_user['username'], directory_user['domain'])
        attributes = self.get_user_attributes(directory_user)
        # check whether the country is set in the directory, use default if not
        country = directory_user['country']
        if not country:
            country = self.options['default_country_code']
        if not country:
            if identity_type == user_sync.identity_type.ENTERPRISE_IDENTITY_TYPE:
                # Enterprise users are allowed to have undefined country
                country = 'UD'
            else:
                self.logger.error("User cannot be added without a specified country code: %s", directory_user)
                return None
        attributes['country'] = country
        if attributes.get('firstname') is None:
            attributes.pop('firstname', None)
        if attributes.get('lastname') is None:
            attributes.pop('lastname', None)
        if do_update:
            attributes['option'] = 'updateIfAlreadyExists'
        else:
            attributes['option'] = 'ignoreIfAlreadyExists'
        commands.add_user(attributes)
        if update_username is not None and not console_trusted:
            commands.update_user({"email": directory_user['email'], "username": update_username})
        return commands

    def create_umapi_user(self, user_key, groups_to_add, umapi_info, umapi_connector):
        """
        Add the user to the org on the receiving end of the given umapi connector.
        If the connector is the primary connector, we ask to update the user's attributes because
        we believe the primary org owns the directory where users accounts are.  Otherwise,
        we send the user's attributes over, but we don't update them if the user exists.
        If groups_to_add is specified, and we are managing groups, we give the user those groups.
        If we are pushing, we also remove the user from any mapped groups not in groups_to_add.
        (This way, when we push blindly, we manage the entire set of mapped groups.)
        :type user_key: str
        :type groups_to_add: set
        :type umapi_info: UmapiTargetInfo
        :type umapi_connector: user_sync.connector.umapi.UmapiConnector
        """
        directory_user = self.directory_user_by_user_key[user_key]
        commands = self.create_umapi_commands_for_directory_user(directory_user, self.will_update_user_info(umapi_info),
                                                                 umapi_connector.trusted)
        if not commands:
            return
        if self.will_process_groups():
            if self.push_umapi:
                groups_to_remove = umapi_info.get_mapped_groups() - groups_to_add
                commands.remove_groups(groups_to_remove)
            commands.add_groups(groups_to_add)
        user_key_log_entry = commands.identity_type.join(',').join(
            commands.email if commands.email is not None else commands.username)
        if umapi_connector.trusted:
            self.logger.info('Adding user to umapi %s with user key: %s', umapi_connector.name, user_key_log_entry)
            self.secondary_users_created.add(user_key)
        else:
            self.logger.info('Creating user with user key: %s', user_key_log_entry)
            self.primary_users_created.add(user_key)
        post_sync_user = {
            'type': directory_user['identity_type'],
            'username': directory_user['username'],
            'domain': directory_user['domain'],
            'email': directory_user['email'],
            'country': directory_user['country'],
            'firstname': directory_user['firstname'],
            'lastname': directory_user['lastname'],
        }
        self.post_sync_data.update_umapi_data(umapi_info.name, user_key,
                                              groups_to_add if self.will_process_groups() else [], [], **post_sync_user)
        umapi_connector.send_commands(commands)

    def update_umapi_user(self, umapi_info, user_key, umapi_connector,
                          attributes_to_update=None, groups_to_add=None, groups_to_remove=None,
                          umapi_user=None):
        # Note that the user may exist only in the directory, only in the umapi, or both at this point.
        # When we are updating an Adobe user who has been removed from the directory, we have to be careful to use
        # data from the umapi_user parameter and not try to get information from the directory.
        """
        Send the action to update aspects of an adobe user, like info and groups
        :type umapi_info: UmapiTargetInfo
        :type user_key: str
        :type umapi_connector: user_sync.connector.umapi.UmapiConnector
        :type attributes_to_update: dict
        :type groups_to_add: set(str)
        :type groups_to_remove: set(str)
        :type umapi_user: dict # with type, username, domain, and email entries
        """
        if attributes_to_update or groups_to_add or groups_to_remove:
            self.updated_user_keys.add(user_key)
        if attributes_to_update:
            self.logger.info('Updating info for user key: %s changes: %s', user_key, attributes_to_update)
        if groups_to_add or groups_to_remove:
            if self.is_primary_org(umapi_info):
                self.logger.info('Managing groups for user key: %s added: %s removed: %s',
                                 user_key, groups_to_add, groups_to_remove)
            else:
                self.logger.info('Managing groups in %s for user key: %s added: %s removed: %s',
                                 umapi_info.get_name(), user_key, groups_to_add, groups_to_remove)

        if user_key in self.directory_user_by_user_key:
            directory_user = self.directory_user_by_user_key[user_key]
            identity_type = self.get_identity_type_from_directory_user(directory_user)
        else:
            directory_user = umapi_user
            identity_type = umapi_user.get('type')

        # if user has email-type username and it is different from email address, then we need to
        # override the username with email address
        if '@' in directory_user['username'] and normalize_string(directory_user['email']) != normalize_string(directory_user['username']):
            if groups_to_add or groups_to_remove or attributes_to_update:
                directory_user['username'] = directory_user['email']
            if attributes_to_update and 'email' in attributes_to_update:
                directory_user['email'] = umapi_user['email']
                attributes_to_update['username'] = umapi_user['username']
                directory_user['username'] = umapi_user['email']

        # if email based username on umapi is differ than email on umapi and need to update email, then we need to
        # override the username with email address
        if '@' in umapi_user['username'] and normalize_string(umapi_user['username']) != normalize_string(umapi_user['email']):
            if attributes_to_update and 'email' in attributes_to_update:
                directory_user['email'] = umapi_user['email']
                directory_user['username'] = umapi_user['email']

        self.post_sync_data.update_umapi_data(umapi_info.name, user_key, groups_to_add, groups_to_remove,
                                              **attributes_to_update)
        commands = user_sync.connector.umapi.Commands(identity_type, directory_user['email'],
                                                      directory_user['username'], directory_user['domain'])
        commands.update_user(attributes_to_update)
        commands.remove_groups(groups_to_remove)
        commands.add_groups(groups_to_add)
        umapi_connector.send_commands(commands)

    def update_umapi_users_for_connector(self, umapi_info, umapi_connector):
        """
        This is the main function that goes over adobe users and looks for and processes differences.
        It is called with a particular organization that it should manage groups against.
        It returns a map from user keys to adobe groups:
            the keys are the user keys of all the selected directory users that don't exist in the target umapi;
            the value for each key is the set of adobe groups in this umapi that the created user should be put into.
        The use of this return value by the caller is to create the user and add him to the right groups.
        :type umapi_info: UmapiTargetInfo
        :type umapi_connector: user_sync.connector.umapi.UmapiConnector
        :rtype: map(string, set)
        """
        filtered_directory_user_by_user_key = self.filtered_directory_user_by_user_key

        # the way we construct the return value is to start with a map from all directory users
        # to their groups in this umapi, make a copy, and pop off any adobe users we find.
        # That way, any key/value pairs left in the map are the unmatched adobe users and their groups.
        user_to_group_map = umapi_info.get_desired_groups_by_user_key()
        user_to_group_map = {} if user_to_group_map is None else user_to_group_map.copy()

        # compute all static options before looping over users
        in_primary_org = self.is_primary_org(umapi_info)
        update_user_info = self.will_update_user_info(umapi_info)
        process_groups = self.will_process_groups()

        # prepare the strays map if we are going to be processing them
        if self.will_process_strays:
            self.add_stray(umapi_info.get_name(), None)

        if self.options['adobe_group_filter'] is not None:
            umapi_users = self.get_umapi_user_in_groups(umapi_info, umapi_connector, self.options['adobe_group_filter'])
        else:
            umapi_users = umapi_connector.iter_users()
        # Walk all the adobe users, getting their group data, matching them with directory users,
        # and adjusting their attribute and group data accordingly.
        for umapi_user in umapi_users:
            # let save adobeID users to a seperate list
            self.filter_adobeID_user(umapi_user)
            # get the basic data about this user; initialize change markers to "no change"
            user_key = self.get_umapi_user_key(umapi_user)
            if not user_key:
                self.logger.warning("Ignoring umapi user with empty user key: %s", umapi_user)
                continue
            if umapi_info.get_umapi_user(user_key) is not None:
                self.logger.debug("Ignoring umapi user. This user has already been processed: %s", umapi_user)
                continue
            umapi_info.add_umapi_user(user_key, umapi_user)
            self.post_sync_data.update_umapi_data(None, user_key, [], [], **umapi_user)
            attribute_differences = {}
            current_groups = self.normalize_groups(umapi_user.get('groups'))
            groups_to_add = set()
            groups_to_remove = set()

            # If this adobe user matches any directory user, pop them out of the
            # map because we know they don't need to be created.
            # Also, keep track of the mapped groups for the directory user
            # so we can update the adobe user's groups as needed.
            desired_groups = user_to_group_map.pop(user_key, None) or set()

            # check for excluded users
            if self.is_umapi_user_excluded(in_primary_org, user_key, current_groups):
                continue

            self.map_email_override(umapi_user)

            directory_user = filtered_directory_user_by_user_key.get(user_key)
            if directory_user is None:
                # There's no selected directory user matching this adobe user
                # so we mark this adobe user as a stray, and we mark him
                # for removal from any mapped groups.
                if self.exclude_strays:
                    self.logger.debug("Excluding Adobe-only user: %s", user_key)
                    self.excluded_user_count += 1
                elif self.will_process_strays:
                    self.logger.debug("Found Adobe-only user: %s", user_key)
                    self.add_stray(umapi_info.get_name(), user_key,
                                   None if not process_groups else current_groups & umapi_info.get_mapped_groups())
            else:
                # There is a selected directory user who matches this adobe user,
                # so mark any changed umapi attributes,
                # and mark him for addition and removal of the appropriate mapped groups
                if update_user_info or process_groups:
                    self.logger.debug("Adobe user matched on customer side: %s", user_key)
                if update_user_info:
                    attribute_differences = self.get_user_attribute_difference(directory_user, umapi_user)
                if process_groups:
                    groups_to_add = desired_groups - current_groups
                    groups_to_remove = (current_groups - desired_groups) & umapi_info.get_mapped_groups()

            # Finally, execute the attribute and group adjustments
            self.update_umapi_user(umapi_info, user_key, umapi_connector,
                                   attribute_differences, groups_to_add, groups_to_remove, umapi_user)

        # mark the umapi's adobe users as processed and return the remaining ones in the map
        umapi_info.set_umapi_users_loaded()
        return user_to_group_map

    def map_email_override(self, umapi_user):
        """
        for users with email-type usernames that don't match the email address, we need to add some
        special cases to update and disentitle users
        :param umapi_user: dict
        :return:
        """
        email = umapi_user.get('email', '')
        username = umapi_user.get('username', '')
        if '@' in username and username != email:
            self.email_override[username] = email

    @staticmethod
    def get_umapi_user_in_groups(umapi_info, umapi_connector, groups):
        umapi_users_iters = []
        for group in groups:
            if group.get_umapi_name() == umapi_info.get_name():
                umapi_users_iters.append(umapi_connector.iter_users(in_group=group.get_group_name()))
        return chain.from_iterable(umapi_users_iters)

    def is_umapi_user_excluded(self, in_primary_org, user_key, current_groups):
        if in_primary_org:
            self.primary_user_count += 1
            # in the primary umapi, we actually check the exclusion conditions
            identity_type, username, domain = self.parse_user_key(user_key)
            if identity_type in self.exclude_identity_types:
                self.logger.debug("Excluding adobe user (due to type): %s", user_key)
                self.excluded_user_count += 1
                return True
            if len(current_groups & self.exclude_groups) > 0:
                self.logger.debug("Excluding adobe user (due to group): %s", user_key)
                self.excluded_user_count += 1
                return True
            for re_ in self.exclude_users:
                if re_.match(username):
                    self.logger.debug("Excluding adobe user (due to name): %s", user_key)
                    self.excluded_user_count += 1
                    return True
            self.included_user_keys.add(user_key)
            return False
        else:
            # in all other umapis, we exclude every user that
            #  doesn't match an included user from the primary umapi
            return user_key not in self.included_user_keys

    def filter_adobeID_user(self, umapi_user):
        id_type = self.get_identity_type_from_umapi_user(umapi_user)
        if id_type == user_sync.identity_type.ADOBEID_IDENTITY_TYPE:
            self.adobeid_user_by_email[normalize_string(umapi_user['email'])] = umapi_user

    def is_adobeID_email_exist(self, email):
        return bool(self.adobeid_user_by_email.get(normalize_string(email)))

    @staticmethod
    def normalize_groups(group_names):
        """
        :type group_names: iterator(str)
        :rtype set(str)
        """
        result = set()
        if group_names is not None:
            for group_name in group_names:
                normalized_group_name = normalize_string(group_name)
                result.add(normalized_group_name)
        return result


    def get_user_attribute_difference(self, directory_user, umapi_user):
        differences = {}
        attributes = self.get_user_attributes(directory_user)
        for key, value in six.iteritems(attributes):
            umapi_value = umapi_user.get(key)
            if key == 'email':
                diff = normalize_string(value) != normalize_string(umapi_value)
            else:
                diff = value != umapi_value
            if diff:
                differences[key] = value
        return differences

    def get_directory_user_key(self, directory_user):
        """
        Identity-type aware user key management for directory users
        :type directory_user: dict
        """
        id_type = self.get_identity_type_from_directory_user(directory_user)
        return self.get_user_key(id_type, directory_user['username'], directory_user['domain'], directory_user['email'])

    def get_umapi_user_key(self, umapi_user):
        """
        Identity-type aware user key management for adobe users
        :type umapi_user: dict
        """
        id_type = self.get_identity_type_from_umapi_user(umapi_user)
        if id_type == user_sync.identity_type.ADOBEID_IDENTITY_TYPE:
            return self.get_user_key(id_type, '', '', umapi_user['email'])
        else:
            return self.get_user_key(id_type, umapi_user['username'], umapi_user['domain'], umapi_user['email'])

    def get_user_key(self, id_type, username, domain, email=None):
        """
        Construct the user key for a directory or adobe user.
        The user key is the stringification of the tuple (id_type, username, domain)
        but the domain part is left empty if the username is an email address.
        If the parameters are invalid, None is returned.
        :param username: (required) username of the user, can be his email
        :param domain: (optional) domain of the user
        :param email: (optional) email of the user
        :param id_type: (required) id_type of the user
        :return: string "id_type,username,domain" (or None)
        :rtype: str
        """
        id_type = user_sync.identity_type.parse_identity_type(id_type)
        email = normalize_string(email) if email else None
        username = normalize_string(username) or email
        domain = normalize_string(domain)

        if not id_type:
            return None
        if not username:
            return None
        if username.find('@') >= 0:
            domain = ""
        elif not domain:
            return None
        return six.text_type(id_type) + u',' + six.text_type(username) + u',' + six.text_type(domain)

    def parse_user_key(self, user_key):
        """
        Returns the identity_type, username, and domain for the user.
        The domain part is empty except if the username is not an email address.
        :rtype: tuple
        """
        return user_key.split(',')

    def get_username_from_user_key(self, user_key):
        return self.parse_user_key(user_key)[1]

    def read_stray_key_map(self, file_path, delimiter=None):
        """
        Load the users to be removed from a CSV file.  Returns the stray key map.
        :type file_path: str
        :type delimiter: str
        """
        self.logger.info('Reading Adobe-only users from: %s', file_path)
        id_type_column_name = 'type'
        user_column_name = 'username'
        domain_column_name = 'domain'
        ummapi_name_column_name = 'umapi'
        rows = CSVAdapter.read_csv_rows(file_path,
                                        recognized_column_names=[
                                            id_type_column_name, user_column_name, domain_column_name,
                                            ummapi_name_column_name,
                                        ],
                                        logger=self.logger,
                                        delimiter=delimiter)
        for row in rows:
            umapi_name = row.get(ummapi_name_column_name) or PRIMARY_UMAPI_NAME
            id_type = row.get(id_type_column_name)
            user = row.get(user_column_name)
            domain = row.get(domain_column_name)
            user_key = self.get_user_key(id_type, user, domain)
            if user_key:
                self.add_stray(umapi_name, None)
                self.add_stray(umapi_name, user_key)
            else:
                self.logger.error("Invalid input line, ignored: %s", row)
        user_count = len(self.get_stray_keys())
        user_plural = "" if user_count == 1 else "s"
        secondary_count = len(self.stray_key_map) - 1
        if secondary_count > 0:
            umapi_plural = "" if secondary_count == 1 else "s"
            self.logger.info('Read %d Adobe-only user%s for primary umapi, with %d secondary umapi%s',
                             user_count, user_plural, secondary_count, umapi_plural)
        else:
            self.logger.info('Read %d Adobe-only user%s.', user_count, user_plural)

    def write_stray_key_map(self):
        file_path = self.stray_list_output_path
        logger = self.logger
        logger.info('Writing Adobe-only users to: %s', file_path)
        # figure out if we should include a umapi column
        secondary_count = 0
        fieldnames = ['type', 'username', 'domain']
        rows = []
        # count the secondaries, and if there are any add the name as a column
        for umapi_name in self.stray_key_map:
            if umapi_name != PRIMARY_UMAPI_NAME and self.get_stray_keys(umapi_name):
                if not secondary_count:
                    fieldnames.append('umapi')
                secondary_count += 1
        for umapi_name in self.stray_key_map:
            for user_key in self.get_stray_keys(umapi_name):
                id_type, username, domain = self.parse_user_key(user_key)
                umapi = umapi_name if umapi_name else ""
                if secondary_count:
                    row_dict = {'type': id_type, 'username': username, 'domain': domain, 'umapi': umapi}
                else:
                    row_dict = {'type': id_type, 'username': username, 'domain': domain}
                rows.append(row_dict)

        CSVAdapter.write_csv_rows(file_path, fieldnames, rows)
        user_count = len(self.stray_key_map.get(PRIMARY_UMAPI_NAME, []))
        user_plural = "" if user_count == 1 else "s"
        if secondary_count > 0:
            umapi_plural = "" if secondary_count == 1 else "s"
            logger.info('Wrote %d Adobe-only user%s for primary umapi, with %d secondary umapi%s',
                        user_count, user_plural, secondary_count, umapi_plural)
        else:
            logger.info('Wrote %d Adobe-only user%s.', user_count, user_plural)

    def log_after_mapping_hook_scope(self, before_call=None, after_call=None):
        if (before_call is None and after_call is None) or (before_call is not None and after_call is not None):
            raise ValueError("Exactly one of 'before_call', 'after_call' must be passed (and not None)")
        when = 'before' if before_call is not None else 'after'
        if before_call is not None:
            self.logger.debug('.')
            self.logger.debug('Source attrs, %s: %s', when, self.after_mapping_hook_scope['source_attributes'])
            self.logger.debug('Source groups, %s: %s', when, self.after_mapping_hook_scope['source_groups'])
        self.logger.debug('Target attrs, %s: %s', when, self.after_mapping_hook_scope['target_attributes'])
        self.logger.debug('Target groups, %s: %s', when, self.after_mapping_hook_scope['target_groups'])
        if after_call is not None:
            self.logger.debug('Hook storage, %s: %s', when, self.after_mapping_hook_scope['hook_storage'])


class UmapiConnectors(object):
    def __init__(self, primary_connector, secondary_connectors):
        """
        :type primary_connector: user_sync.connector.umapi.UmapiConnector
        :type secondary_connectors: dict(str, user_sync.connector.umapi.UmapiConnector)
        """
        self.primary_connector = primary_connector
        self.secondary_connectors = secondary_connectors

        connectors = [primary_connector]
        connectors.extend(six.itervalues(secondary_connectors))
        self.connectors = connectors

    def get_primary_connector(self):
        return self.primary_connector

    def get_secondary_connectors(self):
        return self.secondary_connectors

    def execute_actions(self):
        while True:
            had_work = False
            for connector in self.connectors:
                action_manager = connector.get_action_manager()
                if action_manager.has_work():
                    action_manager.flush()
                    had_work = True
            if not had_work:
                break


class AdobeGroup(object):
    index_map = {}

    def __init__(self, group_name, umapi_name, index=True):
        """
        :type group_name: str
        :type umapi_name: str
        """
        self.group_name = group_name
        self.umapi_name = umapi_name
        if index:
            AdobeGroup.index_map[(group_name, umapi_name)] = self

    def __eq__(self, other):
        return self.__dict__ == other.__dict__

    def __ne__(self, other):
        return not self.__eq__(other)

    def __hash__(self):
        return hash(frozenset(self.__dict__))

    def __str__(self):
        return str(self.__dict__)

    def get_qualified_name(self):
        prefix = ""
        if self.umapi_name is not None and self.umapi_name != PRIMARY_UMAPI_NAME:
            prefix = self.umapi_name + GROUP_NAME_DELIMITER
        return prefix + self.group_name

    def get_umapi_name(self):
        return self.umapi_name

    def get_group_name(self):
        return self.group_name

    @staticmethod
    def _parse(qualified_name):
        """
        :type qualified_name: str
        :rtype: str, str
        """
        parts = qualified_name.split(GROUP_NAME_DELIMITER)
        group_name = parts.pop()
        umapi_name = GROUP_NAME_DELIMITER.join(parts)
        if len(umapi_name) == 0:
            umapi_name = PRIMARY_UMAPI_NAME
        return group_name, umapi_name

    @classmethod
    def lookup(cls, qualified_name):
        return cls.index_map.get(cls._parse(qualified_name))

    @classmethod
    def create(cls, qualified_name, index=True):
        group_name, umapi_name = cls._parse(qualified_name)
        existing = cls.index_map.get((group_name, umapi_name))
        if existing:
            return existing
        elif len(group_name) > 0:
            return cls(group_name, umapi_name, index)
        else:
            return None

    @classmethod
    def iter_groups(cls):
        return six.itervalues(cls.index_map)


class UmapiTargetInfo(object):
    def __init__(self, name):
        """
        :type name: str
        """
        self.name = name
        self.mapped_groups = set()
        self.non_normalize_mapped_groups = set()
        self.desired_groups_by_user_key = {}
        self.umapi_user_by_user_key = {}
        self.umapi_users_loaded = False
        self.stray_by_user_key = {}
        self.groups_added_by_user_key = {}
        self.groups_removed_by_user_key = {}

        # keep track of auto-mapped additional groups for conflict tracking.
        # if feature is disabled, this dict will be empty
        self.additional_group_map = defaultdict(list)  # type: dict[str, list[str]]

    def get_name(self):
        return self.name

    def add_mapped_group(self, group):
        """
        :type group: str
        """
        normalized_group_name = normalize_string(group)
        self.mapped_groups.add(normalized_group_name)
        self.non_normalize_mapped_groups.add(group)

    def add_additional_group(self, rename_group, member_group):
        normalized_rename_group = normalize_string(rename_group)
        if member_group not in self.additional_group_map[normalized_rename_group]:
            self.additional_group_map[normalized_rename_group].append(member_group)

    def get_additional_group_map(self):
        return self.additional_group_map

    def get_mapped_groups(self):
        return self.mapped_groups

    def get_non_normalize_mapped_groups(self):
        return self.non_normalize_mapped_groups

    def get_desired_groups_by_user_key(self):
        return self.desired_groups_by_user_key

    def get_desired_groups(self, user_key):
        """
        :type user_key: str
        """
        desired_groups = self.desired_groups_by_user_key.get(user_key)
        return desired_groups

    def add_desired_group_for(self, user_key, group):
        """
        :type user_key: str
        :type group: Optional(str)
        """
        desired_groups = self.get_desired_groups(user_key)
        if desired_groups is None:
            self.desired_groups_by_user_key[user_key] = desired_groups = set()
        if group is not None:
            normalized_group_name = normalize_string(group)
            desired_groups.add(normalized_group_name)

    def add_umapi_user(self, user_key, user):
        """
        :type user_key: str
        :type user: dict
        """
        self.umapi_user_by_user_key[user_key] = user

    def iter_umapi_users(self):
        return six.iteritems(self.umapi_user_by_user_key)

    def get_umapi_user(self, user_key):
        """
        :type user_key: str
        """
        return self.umapi_user_by_user_key.get(user_key)

    def set_umapi_users_loaded(self):
        self.umapi_users_loaded = True

    def is_umapi_users_loaded(self):
        return self.umapi_users_loaded

    def __repr__(self):
        return "UmapiTargetInfo('name': %s)" % self.name
