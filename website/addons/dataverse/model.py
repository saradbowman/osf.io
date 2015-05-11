# -*- coding: utf-8 -*-

import httplib as http
import urlparse

import pymongo
from modularodm import fields

from framework.auth.core import _get_current_user
from framework.auth.decorators import Auth
from framework.exceptions import HTTPError
from framework.exceptions import PermissionsError

from website.addons.base import (
    AddonOAuthNodeSettingsBase, AddonOAuthUserSettingsBase, GuidFile, exceptions,
)
from website.addons.dataverse.client import connect_from_settings_or_401
from website.addons.dataverse import serializer

from website.oauth.models import ExternalAccount


class DataverseProvider(object):
    """An alternative to `ExternalProvider` not tied to OAuth"""

    name = 'Dataverse'
    short_name = 'dataverse'
    provider_name = 'dataverse'
    serializer = serializer.DataverseSerializer

    def __init__(self):
        super(DataverseProvider, self).__init__()

        # provide an unauthenticated session by default
        self.account = None

    def __repr__(self):
        return '<{name}: {status}>'.format(
            name=self.__class__.__name__,
            status=self.account.provider_id if self.account else 'anonymous'
        )

    def add_user_auth(self, node_addon, user, external_account_id):

        external_account = ExternalAccount.load(external_account_id)

        if external_account not in user.external_accounts:
            raise HTTPError(http.FORBIDDEN)

        try:
            node_addon.set_auth(external_account, user)
        except PermissionsError:
            raise HTTPError(http.FORBIDDEN)

        node = node_addon.owner
        node.add_log(
            action='dataverse_node_authorized',
            params={
                'project': node.parent_id,
                'node': node._id,
            },
            auth=Auth(user=user),
        )

        result = self.serializer(
            node_settings=node_addon,
            user_settings=user.get_addon(self.provider_name),
        ).serialized_node_settings
        return {'result': result}

    def remove_user_auth(self, node_addon, user):

        node_addon.deauthorize(Auth(user=user))
        node_addon.reload()
        result = self.serializer(
            node_settings=node_addon,
            user_settings=user.get_addon(self.provider_name),
        ).serialized_node_settings
        return {'result': result}


class DataverseFile(GuidFile):

    __indices__ = [
        {
            'key_or_list': [
                ('node', pymongo.ASCENDING),
                ('file_id', pymongo.ASCENDING),
            ],
            'unique': True,
        }
    ]

    file_id = fields.StringField(required=True, index=True)

    @property
    def waterbutler_path(self):
        return '/' + self.file_id

    @property
    def provider(self):
        return 'dataverse'

    @property
    def version_identifier(self):
        return 'version'

    @property
    def unique_identifier(self):
        return self.file_id

    def enrich(self, save=True):
        super(DataverseFile, self).enrich(save)

        # Check permissions
        user = _get_current_user()
        if not self.node.can_edit(user=user):
            try:
                # Users without edit permission can only see published files
                if not self._metadata_cache['extra']['hasPublishedVersion']:
                    raise exceptions.FileDoesntExistError
            except (KeyError, IndexError):
                pass


class AddonDataverseUserSettings(AddonOAuthUserSettingsBase):

    oauth_provider = DataverseProvider
    serializer = serializer.DataverseSerializer

    # Legacy Fields
    api_token = fields.StringField()
    dataverse_username = fields.StringField()
    encrypted_password = fields.StringField()

    # TODO: Verify auth?


class AddonDataverseNodeSettings(AddonOAuthNodeSettingsBase):

    oauth_provider = DataverseProvider
    serializer = serializer.DataverseSerializer

    dataverse_alias = fields.StringField()
    dataverse = fields.StringField()
    dataset_doi = fields.StringField()
    _dataset_id = fields.StringField()
    dataset = fields.StringField()

    # Legacy fields
    study_hdl = fields.StringField()    # Now dataset_doi
    study = fields.StringField()        # Now dataset

    user_settings = fields.ForeignField(
        'addondataverseusersettings', backref='authorized'
    )

    # Legacy settings objects won't have IDs
    @property
    def dataset_id(self):
        if self._dataset_id is None:
            connection = connect_from_settings_or_401(self.user_settings)
            dataverse = connection.get_dataverse(self.dataverse_alias)
            dataset = dataverse.get_dataset_by_doi(self.dataset_doi)
            self._dataset_id = dataset.id
            self.save()
        return self._dataset_id

    @dataset_id.setter
    def dataset_id(self, value):
        self._dataset_id = value

    @property
    def complete(self):
        return bool(self.has_auth and self.dataset_doi is not None)

    def find_or_create_file_guid(self, path):
        file_id = path.strip('/') if path else ''
        return DataverseFile.get_or_create(node=self.owner, file_id=file_id)

    def deauthorize(self, auth, add_log=True):
        """Remove user authorization from this node and log the event."""
        self.dataverse_alias = None
        self.dataverse = None
        self.dataset_doi = None
        self.dataset_id = None
        self.dataset = None
        self.clear_auth()  # Also performs a save

        if add_log:
            node = self.owner
            self.owner.add_log(
                action='dataverse_node_deauthorized',
                params={
                    'project': node.parent_id,
                    'node': node._id,
                },
                auth=auth,
            )

    def serialize_waterbutler_credentials(self):
        if not self.has_auth:
            raise exceptions.AddonError('Addon is not authorized')
        return {'token': self.external_account.oauth_secret}

    def serialize_waterbutler_settings(self):
        return {
            'host': self.external_account.oauth_key,
            'doi': self.dataset_doi,
            'id': self.dataset_id,
            'name': self.dataset,
        }

    def create_waterbutler_log(self, auth, action, metadata):
        path = metadata['path']
        if 'name' in metadata:
            name = metadata['name']
        else:
            query_string = urlparse.urlparse(metadata['full_path']).query
            name = urlparse.parse_qs(query_string).get('name')

        url = self.owner.web_url_for('addon_view_or_download_file', path=path, provider='dataverse')
        self.owner.add_log(
            'dataverse_{0}'.format(action),
            auth=auth,
            params={
                'project': self.owner.parent_id,
                'node': self.owner._id,
                'dataset': self.dataset,
                'filename': name,
                'urls': {
                    'view': url,
                    'download': url + '?action=download'
                },
            },
        )

    ##### Callback overrides #####

    # Note: Registering Dataverse content is disabled for now
    def before_register_message(self, node, user):
        """Return warning text to display if user auth will be copied to a
        registration.
        """
        category = node.project_or_component
        if self.user_settings and self.user_settings.has_auth:
            return (
                u'The contents of Dataverse add-ons cannot be registered at this time; '
                u'the Dataverse dataset linked to this {category} will not be included '
                u'as part of this registration.'
            ).format(**locals())

    # backwards compatibility
    before_register = before_register_message

    def before_fork_message(self, node, user):
        """Return warning text to display if user auth will be copied to a
        fork.
        """
        category = node.project_or_component
        if self.user_settings and self.user_settings.owner == user:
            return ('Because you have authorized the Dataverse add-on for this '
                '{category}, forking it will also transfer your authentication '
                'to the forked {category}.').format(category=category)

        else:
            return ('Because the Dataverse add-on has been authorized by a different '
                    'user, forking it will not transfer authentication to the forked '
                    '{category}.').format(category=category)

    # backwards compatibility
    before_fork = before_fork_message

    def before_remove_contributor_message(self, node, removed):
        """Return warning text to display if removed contributor is the user
        who authorized the Dataverse addon
        """
        if self.user_settings and self.user_settings.owner == removed:
            category = node.project_or_component
            name = removed.fullname
            return ('The Dataverse add-on for this {category} is authenticated by {name}. '
                    'Removing this user will also remove write access to Dataverse '
                    'unless another contributor re-authenticates the add-on.'
                    ).format(**locals())

    # backwards compatibility
    before_remove_contributor = before_remove_contributor_message

    # Note: Registering Dataverse content is disabled for now
    # def after_register(self, node, registration, user, save=True):
    #     """After registering a node, copy the user settings and save the
    #     chosen folder.
    #
    #     :return: A tuple of the form (cloned_settings, message)
    #     """
    #     clone, message = super(AddonDataverseNodeSettings, self).after_register(
    #         node, registration, user, save=False
    #     )
    #     # Copy user_settings and add registration data
    #     if self.has_auth and self.folder is not None:
    #         clone.user_settings = self.user_settings
    #         clone.registration_data['folder'] = self.folder
    #     if save:
    #         clone.save()
    #     return clone, message

    def after_fork(self, node, fork, user, save=True):
        """After forking, copy user settings if the user is the one who authorized
        the addon.

        :return: A tuple of the form (cloned_settings, message)
        """
        clone, _ = super(AddonDataverseNodeSettings, self).after_fork(
            node=node, fork=fork, user=user, save=False
        )

        if self.user_settings and self.user_settings.owner == user:
            clone.user_settings = self.user_settings
            message = (
                'Dataverse authorization copied to forked {cat}.'
            ).format(
                cat=fork.project_or_component
            )
        else:
            message = (
                'Dataverse authorization not copied to forked {cat}. You may '
                'authorize this fork on the <a href="{url}">Settings</a> '
                'page.'
            ).format(
                url=fork.web_url_for('node_setting'),
                cat=fork.project_or_component
            )
        if save:
            clone.save()
        return clone, message

    def after_remove_contributor(self, node, removed, auth=None):
        """If the removed contributor was the user who authorized the Dataverse
        addon, remove the auth credentials from this node.
        Return the message text that will be displayed to the user.
        """
        if self.user_settings and self.user_settings.owner == removed:
            self.user_settings = None
            self.save()

            message = (
                u'Because the Dataverse add-on for {category} "{title}" was authenticated '
                u'by {user}, authentication information has been deleted.'
            ).format(
                category=node.category_display,
                title=node.title,
                user=removed.fullname
            )

            if not auth or auth.user != removed:
                url = node.web_url_for('node_setting')
                message += (
                    u' You can re-authenticate on the <a href="{url}">Settings</a> page.'
                ).format(url=url)
            #
            return message

    def after_delete(self, node, user):
        self.deauthorize(Auth(user=user), add_log=True)
        self.save()
