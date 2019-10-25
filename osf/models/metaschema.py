# -*- coding: utf-8 -*-
from django.db import models
import jsonschema

from website.util import api_v2_url

from osf.models.base import BaseModel, ObjectIDMixin
from osf.utils.datetime_aware_jsonfield import DateTimeAwareJSONField
from osf.utils.migrations import build_flattened_jsonschema
from osf.exceptions import ValidationValueError, ValidationError

from website.project.metadata.utils import create_jsonschema_from_metaschema

SCHEMABLOCK_TYPES = [
    ('page-heading', 'page-heading'),
    ('section-heading', 'section-heading'),
    ('subsection-heading', 'subsection-heading'),
    ('paragraph', 'paragraph'),
    ('question-label', 'question-label'),
    ('short-text-input', 'short-text-input'),
    ('long-text-input', 'long-text-input'),
    ('file-input', 'file-input'),
    ('contributors-input', 'contributors-input'),
    ('single-select-input', 'single-select-input'),
    ('multi-select-input', 'multi-select-input'),
    ('select-input-option', 'select-input-option'),
    ('select-other-option', 'select-other-option'),
]


class AbstractSchemaManager(models.Manager):
    def get_latest_version(self, name, only_active=True):
        """
        Return the latest version of the given schema
        :param str only_active: Only returns the latest active schema
        :return schema
        """
        schemas = self.filter(name=name, active=True) if only_active else self.filter(name=name)
        sorted_schemas = schemas.order_by('schema_version')
        if sorted_schemas:
            return sorted_schemas.last()
        else:
            return None

    def get_latest_versions(self, only_active=True):
        """
        Returns a queryset of the latest version of each schema
        :param str only_active: Only return active schemas
        :return queryset
        """
        latest_schemas = self.filter(visible=True)
        if only_active:
            latest_schemas = latest_schemas.filter(active=True)
        return latest_schemas.order_by('name', '-schema_version').distinct('name')


class AbstractSchema(ObjectIDMixin, BaseModel):
    name = models.CharField(max_length=255)
    schema = DateTimeAwareJSONField(default=dict)
    category = models.CharField(max_length=255, null=True, blank=True)
    active = models.BooleanField(default=True)  # whether or not the schema accepts submissions
    visible = models.BooleanField(default=True)  # whether or not the schema should be visible in the API and registries search

    # Version of the schema to use (e.g. if questions, responses change)
    schema_version = models.IntegerField()

    objects = AbstractSchemaManager()

    class Meta:
        abstract = True
        unique_together = ('name', 'schema_version')

    def __unicode__(self):
        return '(name={}, schema_version={}, id={})'.format(self.name, self.schema_version, self.id)


class RegistrationSchema(AbstractSchema):
    config = DateTimeAwareJSONField(blank=True, default=dict)
    description = models.TextField(null=True, blank=True)

    @property
    def _config(self):
        return self.schema.get('config', {})

    @property
    def requires_approval(self):
        return self._config.get('requiresApproval', False)

    @property
    def fulfills(self):
        return self._config.get('fulfills', [])

    @property
    def messages(self):
        return self._config.get('messages', {})

    @property
    def requires_consent(self):
        return self._config.get('requiresConsent', False)

    @property
    def has_files(self):
        return self._config.get('hasFiles', False)

    @property
    def absolute_api_v2_url(self):
        path = '/schemas/registrations/{}/'.format(self._id)
        return api_v2_url(path)

    @classmethod
    def get_prereg_schema(cls):
        return cls.objects.get(
            name='Prereg Challenge',
            schema_version=2
        )

    def validate_metadata(self, metadata, reviewer=False, required_fields=False):
        """
        Validates registration_metadata field.
        """
        schema = create_jsonschema_from_metaschema(self.schema,
                                                   required_fields=required_fields,
                                                   is_reviewer=reviewer)
        try:
            jsonschema.validate(metadata, schema)
        except jsonschema.ValidationError as e:
            for page in self.schema['pages']:
                for question in page['questions']:
                    if e.relative_schema_path[0] == 'required':
                        raise ValidationError(
                            'For your registration the \'{}\' field is required'.format(question['title'])
                        )
                    elif e.relative_schema_path[0] == 'additionalProperties':
                        raise ValidationError(
                            'For your registration the \'{}\' field is extraneous and not permitted in your response.'.format(question['qid'])
                        )
                    elif e.relative_path[0] == question['qid']:
                        if 'options' in question:
                            raise ValidationError(
                                'For your registration your response to the \'{}\' field is invalid, your response must be one of the provided options.'.format(
                                    question['title'],
                                ),
                            )
                        raise ValidationError(
                            'For your registration your response to the \'{}\' field is invalid.'.format(question['title']),
                        )
            raise ValidationError(e)
        except jsonschema.SchemaError as e:
            raise ValidationValueError(e)
        return

    def validate_registration_responses(self, registration_responses, required_fields=False):
        """
        Validates registration_responses against the cached jsonschema on the RegistrationSchema.
        The `title` of the question is stashed under the description for the particular question property
        for forumulating a more clear error response.
        """
        validation_schema = build_flattened_jsonschema(self, required_fields=required_fields)

        try:
            jsonschema.validate(registration_responses, validation_schema)
        except jsonschema.ValidationError as e:
            properties = validation_schema.get('properties', {})
            relative_path = getattr(e, 'relative_path', None)
            question_id = relative_path[0] if relative_path else ''
            if properties.get(question_id, None):
                question_title = properties.get(question_id).get('description') or question_id
                if e.relative_schema_path[0] == 'required':
                    raise ValidationError(
                        'For your registration the \'{}\' field is required'.format(question_title)
                    )
                elif 'enum' in properties.get(question_id):
                    raise ValidationError(
                        'For your registration, your response to the \'{}\' field is invalid, your response must be one of the provided options.'.format(
                            question_title,
                        ),
                    )
                else:
                    raise ValidationError(
                        'For your registration, your response to the \'{}\' field is invalid. {}'.format(question_title, e.message),
                    )
            raise ValidationError(e.message)
        except jsonschema.SchemaError as e:
            raise ValidationValueError(e.message)
        return True


class FileMetadataSchema(AbstractSchema):

    @property
    def absolute_api_v2_url(self):
        path = '/schemas/files/{}/'.format(self._id)
        return api_v2_url(path)


class RegistrationSchemaBlock(ObjectIDMixin, BaseModel):
    class Meta:
        order_with_respect_to = 'schema'
        unique_together = ('schema', 'registration_response_key')

    schema = models.ForeignKey('RegistrationSchema', related_name='schema_blocks', on_delete=models.CASCADE)
    help_text = models.TextField()
    example_text = models.TextField(null=True)
    # Corresponds to a key in DraftRegistration.registration_responses dictionary
    registration_response_key = models.CharField(max_length=255, db_index=True, null=True, blank=True)
    # A question can be split into multiple schema blocks, but are linked with a schema_block_group_key
    schema_block_group_key = models.CharField(max_length=24, db_index=True, null=True)
    block_type = models.CharField(max_length=31, db_index=True, choices=SCHEMABLOCK_TYPES)
    display_text = models.TextField()
    required = models.BooleanField(default=False)

    @property
    def absolute_api_v2_url(self):
        path = '{}schema_blocks/{}/'.format(self.schema.absolute_api_v2_url, self._id)
        return api_v2_url(path)

    def save(self, *args, **kwargs):
        """
        Allows us to use a unique_together constraint, so each "registration_response_key"
        only appears once for every registration schema.  To do this, we need to save
        empty "registration_response_key"s as null, instead of an empty string.
        """
        self.registration_response_key = self.registration_response_key or None
        return super(RegistrationSchemaBlock, self).save(*args, **kwargs)
