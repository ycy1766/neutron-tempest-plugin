#    Licensed under the Apache License, Version 2.0 (the "License"); you may
#    not use this file except in compliance with the License. You may obtain
#    a copy of the License at
#
#         http://www.apache.org/licenses/LICENSE-2.0
#
#    Unless required by applicable law or agreed to in writing, software
#    distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
#    WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
#    License for the specific language governing permissions and limitations
#    under the License.

from oslo_utils import uuidutils

from tempest.common import utils
from tempest.lib.common.utils import data_utils
from tempest.lib import decorators
from tempest.lib import exceptions as lib_exc

from neutron_tempest_plugin.tap_as_a_service import base


class TapMirrorLportNegativeTestJSON(base.BaseTaasTest):
    """Requests for ``lport`` tap mirrors the API has to refuse.

    The ports used here are not bound to a host, which is enough for the
    refusals below. The successful calls need the port of a running
    instance as remote port and are covered by the ``TestTapMirrorLport``
    scenario test.
    """

    @classmethod
    @utils.requires_ext(extension='tap-mirror-lport', service='network')
    def skip_checks(cls):
        super().skip_checks()

    @classmethod
    def resource_setup(cls):
        super().resource_setup()
        cls.network = cls.create_network()
        cls.create_subnet(cls.network)
        cls.source_port = cls.create_port(cls.network)
        cls.remote_port = cls.create_port(cls.network)
        cls.lport = 'lport'
        cls.both = {'BOTH': None}

    def _assert_refused(self, exception, reason, **kwargs):
        """The create call fails with ``exception`` for the given reason.

        Several checks answer with the same status code (the remote port
        used here is not bound, which is refused as well), so the status
        alone would not tell which check refused the request.
        """
        exc = self.assertRaises(exception, self.create_tap_mirror, **kwargs)
        self.assertIn(reason, str(exc))
        mirrors = self.tap_mirrors_client.list_tap_mirrors()['tap_mirrors']
        self.assertNotIn(self.source_port['id'],
                         [m['port_id'] for m in mirrors])

    @decorators.attr(type='negative')
    @decorators.idempotent_id('0c7a34d0-2b0e-4b6f-9a0d-6c7f0c1d5e11')
    def test_create_lport_mirror_without_remote_port(self):
        self._assert_refused(lib_exc.BadRequest,
                             "requires remote_port_id",
                             port_id=self.source_port['id'],
                             directions=self.both,
                             mirror_type=self.lport)

    @decorators.attr(type='negative')
    @decorators.idempotent_id('5d0f3f0e-6f0a-4f43-8d55-2f8f6a1b7c22')
    def test_create_lport_mirror_with_remote_ip(self):
        self._assert_refused(lib_exc.BadRequest,
                             "does not accept remote_ip",
                             port_id=self.source_port['id'],
                             directions=self.both,
                             remote_ip='192.0.2.10',
                             remote_port_id=self.remote_port['id'],
                             mirror_type=self.lport)

    @decorators.attr(type='negative')
    @decorators.idempotent_id('8e2b7c57-93b1-4f3e-a2c8-0d3e9b4a6f33')
    def test_create_lport_mirror_with_tunnel_id(self):
        self._assert_refused(lib_exc.BadRequest,
                             "does not accept a tunnel ID",
                             port_id=self.source_port['id'],
                             directions={'IN': 101},
                             remote_port_id=self.remote_port['id'],
                             mirror_type=self.lport)

    @decorators.attr(type='negative')
    @decorators.idempotent_id('b1c94a6e-0f6d-4a57-b0a4-7b1f5e2d8c44')
    def test_create_lport_mirror_to_the_source_port(self):
        self._assert_refused(lib_exc.BadRequest,
                             "must differ",
                             port_id=self.source_port['id'],
                             directions=self.both,
                             remote_port_id=self.source_port['id'],
                             mirror_type=self.lport)

    @decorators.attr(type='negative')
    @decorators.idempotent_id('c47e9d15-3a8b-4c0f-8e61-9a2d6f3b1e55')
    def test_create_lport_mirror_to_a_missing_port(self):
        missing = uuidutils.generate_uuid()
        self._assert_refused(lib_exc.NotFound, missing,
                             port_id=self.source_port['id'],
                             directions=self.both,
                             remote_port_id=missing,
                             mirror_type=self.lport)

    @decorators.attr(type='negative')
    @decorators.idempotent_id('d93f1b28-5c4e-4d7a-9f02-4e8a7c5d2f66')
    def test_create_lport_mirror_to_a_port_of_another_project(self):
        # A project user can not see the port of another project, so the
        # API answers as for a missing port.
        network = self.admin_networks_client.create_network(
            name=data_utils.rand_name('other-project'))['network']
        self.addCleanup(self.admin_networks_client.delete_network,
                        network['id'])
        port = self.admin_ports_client.create_port(
            network_id=network['id'])['port']
        self.addCleanup(self.admin_ports_client.delete_port, port['id'])
        self._assert_refused(lib_exc.NotFound, port['id'],
                             port_id=self.source_port['id'],
                             directions=self.both,
                             remote_port_id=port['id'],
                             mirror_type=self.lport)

    @decorators.attr(type='negative')
    @decorators.idempotent_id('e6a85c39-7d1f-4e2b-a413-5f9b8d6e3a77')
    def test_create_lport_mirror_to_an_unbound_port(self):
        self._assert_refused(lib_exc.BadRequest,
                             "is not bound to a host",
                             port_id=self.source_port['id'],
                             directions=self.both,
                             remote_port_id=self.remote_port['id'],
                             mirror_type=self.lport)

    @decorators.attr(type='negative')
    @decorators.idempotent_id('f2b76d4a-8e30-4f1c-b524-6a0c9e7f4b88')
    def test_create_gre_mirror_with_remote_port(self):
        self._assert_refused(lib_exc.BadRequest,
                             "does not accept remote_port_id",
                             port_id=self.source_port['id'],
                             directions={'IN': 101},
                             remote_ip='192.0.2.10',
                             remote_port_id=self.remote_port['id'],
                             mirror_type='gre')
