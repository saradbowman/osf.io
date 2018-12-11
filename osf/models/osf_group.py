from django.apps import apps
from django.db import models
from django.db.models.signals import post_save
from django.dispatch import receiver
from guardian.shortcuts import assign_perm, remove_perm, get_perms, get_objects_for_group, get_group_perms
from guardian.models import GroupObjectPermissionBase, UserObjectPermissionBase
from framework.exceptions import PermissionsError
from framework.auth.core import get_user, Auth

from osf.models import base
from osf.models.mixins import GuardianMixin, Loggable
from osf.models import AbstractNode, OSFUser, NodeLog
from osf.models.osf_grouplog import OSFGroupLog
from osf.utils.permissions import ADMIN, MANAGER, MEMBER, MANAGE, reduce_permissions
from osf.utils import sanitize
from website.project import signals as project_signals
from website.osf_groups import signals as group_signals
from website.util import api_v2_url


class OSFGroup(GuardianMixin, Loggable, base.ObjectIDMixin, base.BaseModel):
    """
    OSFGroup model.  When an OSFGroup is created, a manager and member Django group are created.
    Managers belong to both manager and member groups.  Members belong to member group only.

    The OSFGroup's Django member group is given permissions to nodes.
    """

    name = models.TextField(blank=False)
    creator = models.ForeignKey(OSFUser,
                                db_index=True,
                                related_name='osfgroups_created',
                                on_delete=models.SET_NULL,
                                null=True, blank=True)

    groups = {
        'member': ('member_group',),
        'manager': ('manage_group',),
    }
    group_format = 'osfgroup_{self.id}_{group}'

    @property
    def _primary_key(self):
        return self._id

    def __unicode__(self):
        return 'OSFGroup_{}_{}'.format(self.id, self.name)

    class Meta:
        permissions = (
            ('member_group', 'Has group membership'),
            ('manage_group', 'Can manage group membership'),
        )

    @property
    def manager_group(self):
        return self.get_group(MANAGER)

    @property
    def member_group(self):
        return self.get_group(MEMBER)

    @property
    def managers(self):
        return self.manager_group.user_set.all()

    @property
    def members(self):
        # Actually a queryset of all users that belong to the OSFGroup since
        # both members and managers are added to the Member Group
        return self.member_group.user_set.all()

    @property
    def members_only(self):
        # Users that are truly members-only and not managers
        return self.members.exclude(id__in=self.managers)

    @property
    def nodes(self):
        """
        Returns nodes that the OSF group has permission to
        """
        return get_objects_for_group(self.member_group, 'read_node', AbstractNode)

    @property
    def absolute_api_v2_url(self):
        path = '/groups/{}/'.format(self._id)
        return api_v2_url(path)

    def get_absolute_url(self):
        return self.absolute_api_v2_url

    @property
    def url(self):
        # TODO - front end hasn't been set up
        return '/{}/'.format(self._primary_key)

    def is_member(self, user):
        # Checking group membership instead of permissions, because unregistered
        # members have no perms
        return user in self.members

    def is_manager(self, user):
        # Checking group membership instead of permissions, because unregistered
        # members have no perms
        return user in self.managers

    def _require_manager_permission(self, auth=None):
        if auth and not self.has_permission(auth.user, MANAGE):
            raise PermissionsError('Must be a group manager to modify group membership.')

    def _disabled_user_check(self, user):
        if user.is_disabled:
            raise ValueError('Deactivated users cannot be added to OSF Groups.')

    def _enforce_one_manager(self, user):
        # Group must have at least one registered manager
        if (len(self.managers) == 1 and self.managers[0] == user) or not self.managers.filter(is_registered=True).exclude(id=user.id):
            raise ValueError('Group must have at least one manager.')

    def _get_node_group_perms(self, node, permission):
        permissions = node.groups.get(permission)
        if not permissions:
            raise ValueError('{} is not a valid permission.'.format(permission))
        return permissions

    def send_member_email(self, user, permission, auth=None):
        group_signals.member_added.send(self, user=user, permission=permission, auth=auth)

    def belongs_to_osfgroup(self, user):
        return user in self.members

    def make_member(self, user, auth=None):
        """Add member or downgrade manager to member

        :param user: OSFUser object, intended member
        :param auth: Auth object
        """
        self._require_manager_permission(auth)
        self._disabled_user_check(user)
        adding_member = not self.belongs_to_osfgroup(user)
        if user in self.members_only:
            return False
        self.member_group.user_set.add(user)
        if self.has_permission(user, MANAGE):
            self._enforce_one_manager(user)
            self.manager_group.user_set.remove(user)
            self.add_role_updated_log(user, MEMBER, auth)
        else:
            self.add_log(
                OSFGroupLog.MEMBER_ADDED,
                params={
                    'group': self._id,
                    'user': user._id,
                },
                auth=auth)

        if adding_member:
            self.send_member_email(user, MEMBER, auth)

    def make_manager(self, user, auth=None):
        """Add manager or upgrade member to manager

        :param user: OSFUser object, intended manager
        :param auth: Auth object
        """
        self._require_manager_permission(auth)
        self._disabled_user_check(user)
        adding_member = not self.belongs_to_osfgroup(user)
        if self.is_manager(user):
            return False
        if not self.is_member(user):
            self.add_log(
                OSFGroupLog.MANAGER_ADDED,
                params={
                    'group': self._id,
                    'user': user._id,
                },
                auth=auth)

        else:
            self.add_role_updated_log(user, MANAGER, auth)
        self.manager_group.user_set.add(user)
        self.member_group.user_set.add(user)

        if adding_member:
            self.send_member_email(user, MANAGER, auth)

    def add_unregistered_member(self, fullname, email, auth, role=MEMBER):
        """Add unregistered member or manager to OSFGroup

        :param fullname: string, user fullname
        :param email: email, user email
        :param auth: Auth object
        :param role: string, "member" or "manager", default is member
        """
        OSFUser = apps.get_model('osf.OSFUser')

        user = get_user(email=email)
        if user:
            if user.is_registered or self.is_member(user):
                raise ValueError('User already exists.')
        else:
            user = OSFUser.create_unregistered(fullname=fullname, email=email)

        user.add_unclaimed_record(self, referrer=auth.user, given_name=fullname, email=email)
        user.save()
        if role == MANAGER:
            self.make_manager(user, auth=auth)
        else:
            self.make_member(user, auth=auth)

        return user

    def replace_contributor(self, old, new):
        """
        Replacing unregistered member with a verified user

        Using "replace_contributor" language to mimic Node model
        """
        if not self.is_member(old):
            return False

        # Remove unclaimed record for the group
        if self._id in old.unclaimed_records:
            del old.unclaimed_records[self._id]
            old.save()

        for group_name in self.groups.keys():
            if self.get_group(group_name).user_set.filter(id=old.id).exists():
                self.get_group(group_name).user_set.remove(old)
                self.get_group(group_name).user_set.add(new)
        return True

    def remove_member(self, user, auth=None):
        """Remove member or manager

        :param user: OSFUser object, member/manager to remove
        :param auth: Auth object
        """
        if not (auth and user == auth.user):
            self._require_manager_permission(auth)

        if not self.is_member(user):
            return False
        self._enforce_one_manager(user)
        self.manager_group.user_set.remove(user)
        self.member_group.user_set.remove(user)

        self.add_log(
            OSFGroupLog.MEMBER_REMOVED,
            params={
                'group': self._id,
                'user': user._id,
            },
            auth=auth)

    def set_group_name(self, name, auth=None):
        """Set the name of the group.

        :param str new Name: The new osf group name
        :param auth: Auth object
        """
        self._require_manager_permission(auth)
        new_name = sanitize.strip_html(name)
        # Title hasn't changed after sanitzation, bail out
        if self.name == new_name:
            return False
        old_name = self.name
        self.name = new_name

        self.add_log(
            OSFGroupLog.EDITED_NAME,
            params={
                'group': self._id,
                'name_original': old_name
            },
            auth=auth)

    def add_group_to_node(self, node, permission='write', auth=None):
        """Gives the OSF Group permissions to the node.  Called from node model.

        :param obj AbstractNode
        :param str Highest permission to grant, 'read', 'write', or 'admin'
        :param auth: Auth object
        """
        self._require_manager_permission(auth)

        perms = get_group_perms(self.member_group, node)
        if perms:
            if reduce_permissions(perms) == permission:
                return False
            return self.update_group_permissions_to_node(node, permission, auth)

        permissions = self._get_node_group_perms(node, permission)
        for perm in permissions:
            assign_perm(perm, self.member_group, node)

        params = {
            'group': self._id,
            'node': node._id,
            'permission': permission
        }

        self.add_log(
            OSFGroupLog.NODE_CONNECTED,
            params=params,
            auth=auth)

        self.add_corresponding_node_log(node, NodeLog.GROUP_ADDED, params, auth)
        for user in self.members:
            group_signals.group_added_to_node.send(self, node=node, user=user, permission=permission, auth=auth)

    def update_group_permissions_to_node(self, node, permission='write', auth=None):
        """Updates the OSF Group permissions to the node.  Called from node model.

        :param obj AbstractNode
        :param str Highest permission to grant, 'read', 'write', or 'admin'
        :param auth: Auth object
        """
        current_permissions = reduce_permissions(get_group_perms(self.member_group, node))
        if current_permissions == permission:
            return False
        permissions = self._get_node_group_perms(node, permission)
        to_remove = set(get_perms(self.member_group, node)).difference(permissions)
        for perm in to_remove:
            remove_perm(perm, self.member_group, node)
        for perm in permissions:
            assign_perm(perm, self.member_group, node)
        params = {
            'group': self._id,
            'node': node._id,
            'permission': permission
        }
        self.add_log(
            OSFGroupLog.NODE_PERMS_UPDATED,
            params=params,
            auth=auth
        )

        self.add_corresponding_node_log(node, NodeLog.GROUP_UPDATED, params, auth)

    def remove_group_from_node(self, node, auth):
        """Removes the OSFGroup from the node. Called from node model.

        :param obj AbstractNode
        """
        if not get_group_perms(self.member_group, node):
            return False
        for perm in node.groups[ADMIN]:
            remove_perm(perm, self.member_group, node)
        params = {
            'group': self._id,
            'node': node._id,
        }
        self.add_log(
            OSFGroupLog.NODE_DISCONNECTED,
            params=params,
            auth=auth)

        self.add_corresponding_node_log(node, NodeLog.GROUP_REMOVED, params, auth)
        for user in self.members:
            # send signal to remove this user from project subscriptions,
            # provided the user doesn't have node perms some other way
            project_signals.contributor_removed.send(node, user=user)

    def has_permission(self, user, permission):
        if not user:
            return False
        has_permission = user.has_perm('{}_group'.format(permission), self)
        return has_permission

    def remove_group(self, auth=None):
        """Removes the OSFGroup and associated manager and member django groups

        :param auth: Auth object
        """
        self._require_manager_permission(auth)
        self.member_group.delete()
        self.manager_group.delete()
        self.delete()

    def save(self, *args, **kwargs):
        first_save = not bool(self.pk)
        ret = super(OSFGroup, self).save(*args, **kwargs)
        if first_save:
            self.update_group_permissions()
            self.make_manager(self.creator)
        return ret

    def add_role_updated_log(self, user, role, auth=None):
        self.add_log(
            OSFGroupLog.ROLE_UPDATED,
            params={
                'group': self._id,
                'new_role': role,
                'user': user._id,
            },
            auth=auth)

    def add_corresponding_node_log(self, node, action, params, auth):
        node.add_log(
            action=action,
            params=params,
            auth=auth,
            save=True
        )

    def add_log(self, action, params, auth, log_date=None, save=True):
        user = None
        if auth:
            user = auth.user

        log = OSFGroupLog(
            action=action, user=user,
            params=params, group=self
        )

        log.save()

        self._complete_add_log(log, action, user, save)
        return log


@receiver(post_save, sender=OSFGroup)
def add_project_created_log(sender, instance, created, **kwargs):
    if created:
        log_action = OSFGroupLog.GROUP_CREATED
        log_params = {
            'group': instance._id,
        }

        instance.add_log(
            log_action,
            params=log_params,
            auth=Auth(user=instance.creator),
            log_date=instance.created,
            save=True,
        )


class OSFGroupUserObjectPermission(UserObjectPermissionBase):
    content_object = models.ForeignKey(OSFGroup, on_delete=models.CASCADE)


class OSFGroupGroupObjectPermission(GroupObjectPermissionBase):
    content_object = models.ForeignKey(OSFGroup, on_delete=models.CASCADE)