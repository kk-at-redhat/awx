# -*- coding: utf-8 -*-
import pytest
import json
from unittest import mock

from django.core.exceptions import ValidationError

from awx.api.versioning import reverse

from awx.main.models import InventorySource, Inventory, ActivityStream


@pytest.fixture
def scm_inventory(inventory, project):
    with mock.patch('awx.main.models.unified_jobs.UnifiedJobTemplate.update'):
        inventory.inventory_sources.create(name='foobar', source='scm', source_project=project)
    return inventory


@pytest.mark.django_db
def test_inventory_source_notification_on_cloud_only(get, post, inventory_source_factory, user, notification_template):
    u = user('admin', True)

    cloud_is = inventory_source_factory("ec2")
    cloud_is.source = "ec2"
    cloud_is.save()

    not_is = inventory_source_factory("not_ec2")

    url = reverse('api:inventory_source_notification_templates_success_list', kwargs={'pk': not_is.id})
    response = post(url, dict(id=notification_template.id), u)
    assert response.status_code == 400


@pytest.mark.django_db
def test_inventory_source_unique_together_with_inv(inventory_factory):
    inv1 = inventory_factory('foo')
    inv2 = inventory_factory('bar')
    is1 = InventorySource(name='foo', source='file', inventory=inv1)
    is1.save()
    is2 = InventorySource(name='foo', source='file', inventory=inv1)
    with pytest.raises(ValidationError):
        is2.validate_unique()
    is2 = InventorySource(name='foo', source='file', inventory=inv2)
    is2.validate_unique()


@pytest.mark.django_db
def test_inventory_host_name_unique(scm_inventory, post, admin_user):
    inv_src = scm_inventory.inventory_sources.first()
    inv_src.groups.create(name='barfoo', inventory=scm_inventory)
    resp = post(
        reverse('api:inventory_hosts_list', kwargs={'pk': scm_inventory.id}),
        {
            'name': 'barfoo',
            'inventory_id': scm_inventory.id,
        },
        admin_user,
        expect=400,
    )

    assert resp.status_code == 400
    assert "A Group with that name already exists." in json.dumps(resp.data)


@pytest.mark.django_db
def test_inventory_host_list_ordering(scm_inventory, get, admin_user):
    # create 3 hosts, hit the inventory host list view 3 times and get the order visible there each time and compare
    inv_src = scm_inventory.inventory_sources.first()
    host1 = inv_src.hosts.create(name='1', inventory=scm_inventory)
    host2 = inv_src.hosts.create(name='2', inventory=scm_inventory)
    host3 = inv_src.hosts.create(name='3', inventory=scm_inventory)
    expected_ids = [host1.id, host2.id, host3.id]
    resp = get(
        reverse('api:inventory_hosts_list', kwargs={'pk': scm_inventory.id}),
        admin_user,
    ).data['results']
    host_list = [host['id'] for host in resp]
    assert host_list == expected_ids


@pytest.mark.django_db
def test_inventory_group_name_unique(scm_inventory, post, admin_user):
    inv_src = scm_inventory.inventory_sources.first()
    inv_src.hosts.create(name='barfoo', inventory=scm_inventory)
    resp = post(
        reverse('api:inventory_groups_list', kwargs={'pk': scm_inventory.id}),
        {
            'name': 'barfoo',
            'inventory_id': scm_inventory.id,
        },
        admin_user,
        expect=400,
    )

    assert resp.status_code == 400
    assert "A Host with that name already exists." in json.dumps(resp.data)


@pytest.mark.django_db
def test_inventory_group_list_ordering(scm_inventory, get, put, admin_user):
    # create 3 groups, hit the inventory groups list view 3 times and get the order visible there each time and compare
    inv_src = scm_inventory.inventory_sources.first()
    group1 = inv_src.groups.create(name='1', inventory=scm_inventory)
    group2 = inv_src.groups.create(name='2', inventory=scm_inventory)
    group3 = inv_src.groups.create(name='3', inventory=scm_inventory)
    expected_ids = [group1.id, group2.id, group3.id]
    group_ids = {}
    for x in range(3):
        resp = get(
            reverse('api:inventory_groups_list', kwargs={'pk': scm_inventory.id}),
            admin_user,
        ).data['results']
        group_ids[x] = [group['id'] for group in resp]
    assert group_ids[0] == group_ids[1] == group_ids[2] == expected_ids


@pytest.mark.parametrize("role_field,expected_status_code", [(None, 403), ('admin_role', 200), ('update_role', 403), ('adhoc_role', 403), ('use_role', 403)])
@pytest.mark.django_db
def test_edit_inventory(put, inventory, alice, role_field, expected_status_code):
    data = {
        'organization': inventory.organization.id,
        'name': 'New name',
        'description': 'Hello world',
    }
    if role_field:
        getattr(inventory, role_field).members.add(alice)
    put(reverse('api:inventory_detail', kwargs={'pk': inventory.id}), data, alice, expect=expected_status_code)


@pytest.mark.django_db
def test_async_inventory_deletion(delete, get, inventory, alice):
    inventory.admin_role.members.add(alice)
    resp = delete(reverse('api:inventory_detail', kwargs={'pk': inventory.id}), alice)
    assert resp.status_code == 202
    assert ActivityStream.objects.filter(operation='delete').exists()

    resp = get(reverse('api:inventory_detail', kwargs={'pk': inventory.id}), alice)
    assert resp.status_code == 200
    assert resp.data.get('pending_deletion') is True


@pytest.mark.django_db
def test_async_inventory_duplicate_deletion_prevention(delete, get, inventory, alice):
    inventory.admin_role.members.add(alice)
    resp = delete(reverse('api:inventory_detail', kwargs={'pk': inventory.id}), alice)
    assert resp.status_code == 202

    resp = delete(reverse('api:inventory_detail', kwargs={'pk': inventory.id}), alice)
    assert resp.status_code == 400
    assert resp.data['error'] == 'Inventory is already pending deletion.'


@pytest.mark.django_db
def test_async_inventory_deletion_deletes_related_jt(delete, get, job_template, inventory, alice, admin):
    job_template.inventory = inventory
    job_template.save()
    assert job_template.inventory == inventory
    inventory.admin_role.members.add(alice)
    resp = delete(reverse('api:inventory_detail', kwargs={'pk': inventory.id}), alice)
    assert resp.status_code == 202

    resp = get(reverse('api:job_template_detail', kwargs={'pk': job_template.id}), admin)
    jdata = json.loads(resp.content)
    assert jdata['inventory'] is None


@pytest.mark.parametrize('order_by', ('extra_vars', '-extra_vars', 'extra_vars,pk', '-extra_vars,pk'))
@pytest.mark.django_db
def test_list_cannot_order_by_unsearchable_field(get, organization, alice, order_by):
    get(reverse('api:job_list'), alice, QUERY_STRING='order_by=%s' % order_by, expect=403)


@pytest.mark.parametrize("role_field,expected_status_code", [(None, 403), ('admin_role', 201), ('update_role', 403), ('adhoc_role', 403), ('use_role', 403)])
@pytest.mark.django_db
def test_create_inventory_group(post, inventory, alice, role_field, expected_status_code):
    data = {
        'name': 'New name',
        'description': 'Hello world',
    }
    if role_field:
        getattr(inventory, role_field).members.add(alice)
    post(reverse('api:inventory_groups_list', kwargs={'pk': inventory.id}), data, alice, expect=expected_status_code)


@pytest.mark.parametrize("role_field,expected_status_code", [(None, 403), ('admin_role', 201), ('update_role', 403), ('adhoc_role', 403), ('use_role', 403)])
@pytest.mark.django_db
def test_create_inventory_group_child(post, group, alice, role_field, expected_status_code):
    data = {
        'name': 'New name',
        'description': 'Hello world',
    }
    if role_field:
        getattr(group.inventory, role_field).members.add(alice)
    post(reverse('api:group_children_list', kwargs={'pk': group.id}), data, alice, expect=expected_status_code)


@pytest.mark.parametrize("role_field,expected_status_code", [(None, 403), ('admin_role', 200), ('update_role', 403), ('adhoc_role', 403), ('use_role', 403)])
@pytest.mark.django_db
def test_edit_inventory_group(put, group, alice, role_field, expected_status_code):
    data = {
        'name': 'New name',
        'description': 'Hello world',
    }
    if role_field:
        getattr(group.inventory, role_field).members.add(alice)
    put(reverse('api:group_detail', kwargs={'pk': group.id}), data, alice, expect=expected_status_code)


@pytest.mark.parametrize("role_field,expected_status_code", [(None, 403), ('admin_role', 201), ('update_role', 403), ('adhoc_role', 403), ('use_role', 403)])
@pytest.mark.django_db
def test_create_inventory_inventory_source(post, inventory, alice, role_field, expected_status_code):
    data = {'source': 'ec2', 'name': 'ec2-inv-source'}
    if role_field:
        getattr(inventory, role_field).members.add(alice)
    post(reverse('api:inventory_inventory_sources_list', kwargs={'pk': inventory.id}), data, alice, expect=expected_status_code)


@pytest.mark.parametrize("role_field,expected_status_code", [(None, 403), ('admin_role', 204), ('update_role', 403), ('adhoc_role', 403), ('use_role', 403)])
@pytest.mark.django_db
def test_delete_inventory_group(delete, group, alice, role_field, expected_status_code):
    if role_field:
        getattr(group.inventory, role_field).members.add(alice)
    delete(reverse('api:group_detail', kwargs={'pk': group.id}), alice, expect=expected_status_code)


@pytest.mark.django_db
def test_create_inventory_smartgroup(post, get, inventory, admin_user, organization):
    data = {'name': 'Group 1', 'description': 'Test Group'}
    smart_inventory = Inventory(name='smart', kind='smart', organization=organization, host_filter='inventory_sources__source=ec2')
    smart_inventory.save()
    post(reverse('api:inventory_groups_list', kwargs={'pk': smart_inventory.id}), data, admin_user)
    resp = get(reverse('api:inventory_groups_list', kwargs={'pk': smart_inventory.id}), admin_user)
    jdata = json.loads(resp.content)

    assert getattr(smart_inventory, 'kind') == 'smart'
    assert jdata['count'] == 0


@pytest.mark.django_db
def test_create_inventory_smart_inventory_sources(post, get, inventory, admin_user, organization):
    data = {'name': 'Inventory Source 1', 'description': 'Test Inventory Source'}
    smart_inventory = Inventory(name='smart', kind='smart', organization=organization, host_filter='inventory_sources__source=ec2')
    smart_inventory.save()
    post(reverse('api:inventory_inventory_sources_list', kwargs={'pk': smart_inventory.id}), data, admin_user)
    resp = get(reverse('api:inventory_inventory_sources_list', kwargs={'pk': smart_inventory.id}), admin_user)
    jdata = json.loads(resp.content)

    assert getattr(smart_inventory, 'kind') == 'smart'
    assert jdata['count'] == 0


@pytest.mark.django_db
def test_urlencode_host_filter(post, admin_user, organization):
    """
    Host filters saved on the model must correspond to the same result
    as when that host_filter is used in the URL as a querystring.
    That means that it must be url-encoded patterns like %22 for quotes
    must be escaped as the string is saved to the model.

    Expected host filter in this test would match a host such as:
    inventory.hosts.create(
        ansible_facts={"ansible_distribution_version": "7.4"}
    )
    """
    # Create smart inventory with host filter that corresponds to querystring
    post(
        reverse('api:inventory_list'),
        data={
            'name': 'smart inventory',
            'kind': 'smart',
            'organization': organization.pk,
            'host_filter': 'ansible_facts__ansible_distribution_version=%227.4%22',
        },
        user=admin_user,
        expect=201,
    )
    # Assert that the saved version of host filter has escaped ""
    si = Inventory.objects.get(name='smart inventory')
    assert si.host_filter == 'ansible_facts__ansible_distribution_version="7.4"'


@pytest.mark.django_db
def test_host_filter_unicode(post, admin_user, organization):
    post(
        reverse('api:inventory_list'),
        data={'name': 'smart inventory', 'kind': 'smart', 'organization': organization.pk, 'host_filter': u'ansible_facts__ansible_distribution=レッドハット'},
        user=admin_user,
        expect=201,
    )
    si = Inventory.objects.get(name='smart inventory')
    assert si.host_filter == u'ansible_facts__ansible_distribution=レッドハット'


@pytest.mark.django_db
@pytest.mark.parametrize("lookup", ['icontains', 'has_keys'])
def test_host_filter_invalid_ansible_facts_lookup(post, admin_user, organization, lookup):
    resp = post(
        reverse('api:inventory_list'),
        data={
            'name': 'smart inventory',
            'kind': 'smart',
            'organization': organization.pk,
            'host_filter': u'ansible_facts__ansible_distribution__{}=cent'.format(lookup),
        },
        user=admin_user,
        expect=400,
    )
    assert 'ansible_facts does not support searching with __{}'.format(lookup) in json.dumps(resp.data)


@pytest.mark.django_db
def test_host_filter_ansible_facts_exact(post, admin_user, organization):
    post(
        reverse('api:inventory_list'),
        data={
            'name': 'smart inventory',
            'kind': 'smart',
            'organization': organization.pk,
            'host_filter': 'ansible_facts__ansible_distribution__exact="CentOS"',
        },
        user=admin_user,
        expect=201,
    )


@pytest.mark.parametrize("role_field,expected_status_code", [(None, 403), ('admin_role', 201), ('update_role', 403), ('adhoc_role', 403), ('use_role', 403)])
@pytest.mark.django_db
def test_create_inventory_host(post, inventory, alice, role_field, expected_status_code):
    data = {
        'name': 'New name',
        'description': 'Hello world',
    }
    if role_field:
        getattr(inventory, role_field).members.add(alice)
    post(reverse('api:inventory_hosts_list', kwargs={'pk': inventory.id}), data, alice, expect=expected_status_code)


@pytest.mark.parametrize(
    "hosts,expected_status_code",
    [
        (1, 201),
        (2, 201),
        (3, 201),
    ],
)
@pytest.mark.django_db
def test_create_inventory_host_with_limits(post, admin_user, inventory, hosts, expected_status_code):
    # The per-Organization host limits functionality should be a no-op on AWX.
    inventory.organization.max_hosts = 2
    inventory.organization.save()
    for i in range(hosts):
        inventory.hosts.create(name="Existing host %i" % i)

    data = {'name': 'New name', 'description': 'Hello world'}
    post(reverse('api:inventory_hosts_list', kwargs={'pk': inventory.id}), data, admin_user, expect=expected_status_code)


@pytest.mark.parametrize("role_field,expected_status_code", [(None, 403), ('admin_role', 201), ('update_role', 403), ('adhoc_role', 403), ('use_role', 403)])
@pytest.mark.django_db
def test_create_inventory_group_host(post, group, alice, role_field, expected_status_code):
    data = {
        'name': 'New name',
        'description': 'Hello world',
    }
    if role_field:
        getattr(group.inventory, role_field).members.add(alice)
    post(reverse('api:group_hosts_list', kwargs={'pk': group.id}), data, alice, expect=expected_status_code)


@pytest.mark.parametrize("role_field,expected_status_code", [(None, 403), ('admin_role', 200), ('update_role', 403), ('adhoc_role', 403), ('use_role', 403)])
@pytest.mark.django_db
def test_edit_inventory_host(put, host, alice, role_field, expected_status_code):
    data = {
        'name': 'New name',
        'description': 'Hello world',
    }
    if role_field:
        getattr(host.inventory, role_field).members.add(alice)
    put(reverse('api:host_detail', kwargs={'pk': host.id}), data, alice, expect=expected_status_code)


@pytest.mark.django_db
def test_edit_inventory_host_with_limits(put, host, admin_user):
    # The per-Organization host limits functionality should be a no-op on AWX.
    inventory = host.inventory
    inventory.organization.max_hosts = 1
    inventory.organization.save()
    inventory.hosts.create(name='Alternate host')

    data = {'name': 'New name', 'description': 'Hello world'}
    put(reverse('api:host_detail', kwargs={'pk': host.id}), data, admin_user, expect=200)


@pytest.mark.parametrize("role_field,expected_status_code", [(None, 403), ('admin_role', 204), ('update_role', 403), ('adhoc_role', 403), ('use_role', 403)])
@pytest.mark.django_db
def test_delete_inventory_host(delete, host, alice, role_field, expected_status_code):
    if role_field:
        getattr(host.inventory, role_field).members.add(alice)
    delete(reverse('api:host_detail', kwargs={'pk': host.id}), alice, expect=expected_status_code)


# See companion test in tests/functional/test_rbac_inventory.py::test_inventory_source_update
@pytest.mark.parametrize("start_access,expected_status_code", [(True, 202), (False, 403)])
@pytest.mark.django_db
def test_inventory_update_access_called(post, inventory_source, alice, mock_access, start_access, expected_status_code):
    with mock_access(InventorySource) as mock_instance:
        mock_instance.can_start = mock.MagicMock(return_value=start_access)
        post(reverse('api:inventory_source_update_view', kwargs={'pk': inventory_source.id}), {}, alice, expect=expected_status_code)
        mock_instance.can_start.assert_called_once_with(inventory_source)


@pytest.mark.django_db
def test_inventory_source_vars_prohibition(post, inventory, admin_user):
    with mock.patch('awx.api.serializers.settings') as mock_settings:
        mock_settings.INV_ENV_VARIABLE_BLOCKED = ('FOOBAR',)
        r = post(
            reverse('api:inventory_source_list'),
            {'name': 'new inv src', 'source_vars': '{\"FOOBAR\": \"val\"}', 'inventory': inventory.pk},
            admin_user,
            expect=400,
        )
    assert 'prohibited environment variable' in r.data['source_vars'][0]
    assert 'FOOBAR' in r.data['source_vars'][0]


@pytest.mark.django_db
@pytest.mark.parametrize('role,expect', [('admin_role', 200), ('use_role', 403), ('adhoc_role', 403), ('read_role', 403)])
def test_action_view_permissions(patch, put, get, inventory, rando, role, expect):
    getattr(inventory, role).members.add(rando)
    url = reverse('api:inventory_variable_data', kwargs={'pk': inventory.pk})
    # read_role and all other roles should be able to view
    get(url=url, user=rando, expect=200)
    patch(url=url, data={"host_filter": "bar"}, user=rando, expect=expect)
    put(url=url, data={"fooooo": "bar"}, user=rando, expect=expect)


@pytest.mark.django_db
class TestInventorySourceCredential:
    def test_need_cloud_credential(self, inventory, admin_user, post):
        """Test that a cloud-based source requires credential"""
        r = post(url=reverse('api:inventory_source_list'), data={'inventory': inventory.pk, 'name': 'foo', 'source': 'openstack'}, expect=400, user=admin_user)
        assert 'Credential is required for a cloud source' in r.data['credential'][0]

    def test_ec2_no_credential(self, inventory, admin_user, post):
        """Test that an ec2 inventory source can be added with no credential"""
        post(url=reverse('api:inventory_source_list'), data={'inventory': inventory.pk, 'name': 'fobar', 'source': 'ec2'}, expect=201, user=admin_user)

    def test_validating_credential_type(self, organization, inventory, admin_user, post):
        """Test that cloud sources must use their respective credential type"""
        from awx.main.models.credential import Credential, CredentialType

        openstack = CredentialType.defaults['openstack']()
        openstack.save()
        os_cred = Credential.objects.create(credential_type=openstack, name='bar', organization=organization)
        r = post(
            url=reverse('api:inventory_source_list'),
            data={'inventory': inventory.pk, 'name': 'fobar', 'source': 'ec2', 'credential': os_cred.pk},
            expect=400,
            user=admin_user,
        )
        assert 'Cloud-based inventory sources (such as ec2)' in r.data['credential'][0]
        assert 'require credentials for the matching cloud service' in r.data['credential'][0]

    def test_vault_credential_not_allowed(self, project, inventory, vault_credential, admin_user, post):
        """Vault credentials cannot be associated via the deprecated field"""
        # TODO: when feature is added, add tests to use the related credentials
        # endpoint for multi-vault attachment
        r = post(
            url=reverse('api:inventory_source_list'),
            data={
                'inventory': inventory.pk,
                'name': 'fobar',
                'source': 'scm',
                'source_project': project.pk,
                'source_path': '',
                'credential': vault_credential.pk,
                'source_vars': 'plugin: a.b.c',
            },
            expect=400,
            user=admin_user,
        )
        assert 'Credentials of type insights and vault' in r.data['credential'][0]
        assert 'disallowed for scm inventory sources' in r.data['credential'][0]

    def test_vault_credential_not_allowed_via_related(self, project, inventory, vault_credential, admin_user, post):
        """Vault credentials cannot be associated via related endpoint"""
        inv_src = InventorySource.objects.create(inventory=inventory, name='foobar', source='scm', source_project=project, source_path='')
        r = post(url=reverse('api:inventory_source_credentials_list', kwargs={'pk': inv_src.pk}), data={'id': vault_credential.pk}, expect=400, user=admin_user)
        assert 'Credentials of type insights and vault' in r.data['msg']
        assert 'disallowed for scm inventory sources' in r.data['msg']

    def test_credentials_relationship_mapping(self, project, inventory, organization, admin_user, post, patch):
        """The credentials relationship is used to manage the cloud credential
        this test checks that replacement works"""
        from awx.main.models.credential import Credential, CredentialType

        openstack = CredentialType.defaults['openstack']()
        openstack.save()
        os_cred = Credential.objects.create(credential_type=openstack, name='bar', organization=organization)
        r = post(
            url=reverse('api:inventory_source_list'),
            data={
                'inventory': inventory.pk,
                'name': 'fobar',
                'source': 'scm',
                'source_project': project.pk,
                'source_path': '',
                'credential': os_cred.pk,
                'source_vars': 'plugin: a.b.c',
            },
            expect=201,
            user=admin_user,
        )
        aws = CredentialType.defaults['aws']()
        aws.save()
        aws_cred = Credential.objects.create(credential_type=aws, name='bar2', organization=organization)
        inv_src = InventorySource.objects.get(pk=r.data['id'])
        assert list(inv_src.credentials.values_list('id', flat=True)) == [os_cred.pk]
        patch(url=inv_src.get_absolute_url(), data={'credential': aws_cred.pk}, expect=200, user=admin_user)
        assert list(inv_src.credentials.values_list('id', flat=True)) == [aws_cred.pk]


@pytest.mark.django_db
class TestControlledBySCM:
    """
    Check that various actions are correctly blocked if object is controlled
    by an SCM follow-project inventory source
    """

    def test_safe_method_works(self, get, options, scm_inventory, admin_user):
        get(scm_inventory.get_absolute_url(), admin_user, expect=200)
        options(scm_inventory.get_absolute_url(), admin_user, expect=200)

    def test_vars_edit_reset(self, patch, scm_inventory, admin_user):
        patch(scm_inventory.get_absolute_url(), {'variables': 'hello: world'}, admin_user, expect=200)

    def test_name_edit_allowed(self, patch, scm_inventory, admin_user):
        patch(scm_inventory.get_absolute_url(), {'variables': '---', 'name': 'newname'}, admin_user, expect=200)

    def test_host_associations_reset(self, post, scm_inventory, admin_user):
        inv_src = scm_inventory.inventory_sources.first()
        h = inv_src.hosts.create(name='barfoo', inventory=scm_inventory)
        g = inv_src.groups.create(name='fooland', inventory=scm_inventory)
        post(reverse('api:host_groups_list', kwargs={'pk': h.id}), {'id': g.id}, admin_user, expect=204)
        post(reverse('api:group_hosts_list', kwargs={'pk': g.id}), {'id': h.id}, admin_user, expect=204)

    def test_group_group_associations_reset(self, post, scm_inventory, admin_user):
        inv_src = scm_inventory.inventory_sources.first()
        g1 = inv_src.groups.create(name='barland', inventory=scm_inventory)
        g2 = inv_src.groups.create(name='fooland', inventory=scm_inventory)
        post(reverse('api:group_children_list', kwargs={'pk': g1.id}), {'id': g2.id}, admin_user, expect=204)

    def test_host_group_delete_reset(self, delete, scm_inventory, admin_user):
        inv_src = scm_inventory.inventory_sources.first()
        h = inv_src.hosts.create(name='barfoo', inventory=scm_inventory)
        g = inv_src.groups.create(name='fooland', inventory=scm_inventory)
        delete(h.get_absolute_url(), admin_user, expect=204)
        delete(g.get_absolute_url(), admin_user, expect=204)

    def test_remove_scm_inv_src(self, delete, scm_inventory, admin_user):
        inv_src = scm_inventory.inventory_sources.first()
        delete(inv_src.get_absolute_url(), admin_user, expect=204)
        assert scm_inventory.inventory_sources.count() == 0

    def test_adding_inv_src_ok(self, post, scm_inventory, project, admin_user):
        post(
            reverse('api:inventory_inventory_sources_list', kwargs={'pk': scm_inventory.id}),
            {
                'name': 'new inv src',
                'source_project': project.pk,
                'source': 'scm',
                'overwrite_vars': True,
                'source_vars': 'plugin: a.b.c',
            },
            admin_user,
            expect=201,
        )

    def test_adding_inv_src_without_proj_access_prohibited(self, post, project, inventory, rando):
        inventory.admin_role.members.add(rando)
        post(
            reverse('api:inventory_inventory_sources_list', kwargs={'pk': inventory.id}),
            {'name': 'new inv src', 'source_project': project.pk, 'source': 'scm', 'overwrite_vars': True, 'source_vars': 'plugin: a.b.c'},
            rando,
            expect=403,
        )


@pytest.mark.django_db
class TestConstructedInventory:
    @pytest.fixture
    def constructed_inventory(self, organization):
        return Inventory.objects.create(name='constructed-test-inventory', kind='constructed', organization=organization)

    def test_get_constructed_inventory(self, constructed_inventory, admin_user, get):
        inv_src = constructed_inventory.inventory_sources.first()
        inv_src.update_cache_timeout = 53
        inv_src.save(update_fields=['update_cache_timeout'])
        r = get(url=reverse('api:constructed_inventory_detail', kwargs={'pk': constructed_inventory.pk}), user=admin_user, expect=200)
        assert r.data['update_cache_timeout'] == 53

    def test_patch_constructed_inventory(self, constructed_inventory, admin_user, patch):
        inv_src = constructed_inventory.inventory_sources.first()
        assert inv_src.update_cache_timeout == 0
        assert inv_src.limit == ''
        r = patch(
            url=reverse('api:constructed_inventory_detail', kwargs={'pk': constructed_inventory.pk}),
            data=dict(update_cache_timeout=54, limit='foobar'),
            user=admin_user,
            expect=200,
        )
        assert r.data['update_cache_timeout'] == 54
        inv_src = constructed_inventory.inventory_sources.first()
        assert inv_src.update_cache_timeout == 54
        assert inv_src.limit == 'foobar'

    def test_patch_constructed_inventory_generated_source_limits_editable_fields(self, constructed_inventory, admin_user, project, patch):
        inv_src = constructed_inventory.inventory_sources.first()
        r = patch(
            url=inv_src.get_absolute_url(),
            data={
                'source': 'scm',
                'source_project': project.pk,
                'source_path': '',
                'source_vars': 'plugin: a.b.c',
            },
            expect=400,
            user=admin_user,
        )
        assert str(r.data['error'][0]) == "Cannot change field 'source' on a constructed inventory source."

        # Make sure it didn't get updated before we got the error
        inv_src_after_err = constructed_inventory.inventory_sources.first()
        assert inv_src.id == inv_src_after_err.id
        assert inv_src.source == inv_src_after_err.source
        assert inv_src.source_project == inv_src_after_err.source_project
        assert inv_src.source_path == inv_src_after_err.source_path
        assert inv_src.source_vars == inv_src_after_err.source_vars

    def test_patch_constructed_inventory_generated_source_allows_source_vars_edit(self, constructed_inventory, admin_user, patch):
        inv_src = constructed_inventory.inventory_sources.first()
        patch(
            url=inv_src.get_absolute_url(),
            data={
                'source_vars': 'plugin: a.b.c',
            },
            expect=200,
            user=admin_user,
        )

        inv_src_after_patch = constructed_inventory.inventory_sources.first()

        # sanity checks
        assert inv_src.id == inv_src_after_patch.id
        assert inv_src.source == 'constructed'
        assert inv_src_after_patch.source == 'constructed'
        assert inv_src.source_vars == ''

        assert inv_src_after_patch.source_vars == 'plugin: a.b.c'

    def test_create_constructed_inventory(self, constructed_inventory, admin_user, post, organization):
        r = post(
            url=reverse('api:constructed_inventory_list'),
            data=dict(name='constructed-inventory-just-created', kind='constructed', organization=organization.id, update_cache_timeout=55, limit='foobar'),
            user=admin_user,
            expect=201,
        )
        pk = r.data['id']
        constructed_inventory = Inventory.objects.get(pk=pk)
        inv_src = constructed_inventory.inventory_sources.first()
        assert inv_src.update_cache_timeout == 55
        assert inv_src.limit == 'foobar'

    def test_get_absolute_url_for_constructed_inventory(self, constructed_inventory, admin_user, get):
        """
        If we are using the normal inventory API endpoint to look at a
        constructed inventory, then we should get a normal inventory API route
        back. If we are accessing it via the special constructed inventory
        endpoint, then we should get that back.
        """

        url_const = reverse('api:constructed_inventory_detail', kwargs={'pk': constructed_inventory.pk})
        url_inv = reverse('api:inventory_detail', kwargs={'pk': constructed_inventory.pk})

        const_r = get(url=url_const, user=admin_user, expect=200)
        inv_r = get(url=url_inv, user=admin_user, expect=200)
        assert const_r.data['url'] == url_const
        assert inv_r.data['url'] == url_inv
        assert inv_r.data['url'] != const_r.data['url']
        assert inv_r.data['related']['constructed_url'] == url_const
        assert const_r.data['related']['constructed_url'] == url_const
