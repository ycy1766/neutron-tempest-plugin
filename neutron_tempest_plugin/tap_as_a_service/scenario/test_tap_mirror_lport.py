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

from neutron_lib._i18n import _
from oslo_log import log as logging

from tempest.common import utils
from tempest import config
from tempest.lib.common.utils import data_utils
from tempest.lib.common.utils import test_utils
from tempest.lib import decorators
from tempest.lib import exceptions as lib_exc

from neutron_tempest_plugin.common import utils as common_utils
from neutron_tempest_plugin.tap_as_a_service.scenario import manager
from neutron_tempest_plugin.tap_as_a_service.scenario import test_tap_mirror

CONF = config.CONF
# The topology helpers of the gre/erspan scenario are reused here.
tm_scenario = test_tap_mirror
LOG = logging.getLogger(__name__)

LPORT = 'lport'
LOG_LOCATION = "/tmp/tcpdump_lport.log"
PING_COUNT = 5
# Seconds to wait for OVN to install or remove the mirror flows.
MIRROR_TIMEOUT = 60
# Seconds to wait for tcpdump to start or to stop on the collector.
CAPTURE_TIMEOUT = 30


class TestTapMirrorLport(manager.BaseTaasScenarioTests):
    """Scenario tests for tap mirrors of type ``lport``.

    vm0 pings vm1 and the port of vm1 is mirrored to the port of a third
    instance, the collector, which takes no part in the conversation and
    sits on its own network. Whatever ICMP between vm0 and vm1 shows up on
    the collector can therefore only be a mirrored copy. The copies are not
    encapsulated. Each traffic test checks that the collector sees nothing
    before the mirror exists, sees the expected direction(s) while it
    exists and sees nothing again once it is deleted.

    .. code-block:: HTML

       +-----------------------+      +--------------+
       |         Net0          |      |    NetMon    |
       +---+---------------+---+      +------+-------+
           |               |                 |
       +---+---+       +---+---+       +-----+-----+
       |  vm0  | ping  |  vm1  | lport | collector |
       |       +------>+ (src) +------>+ (tcpdump) |
       +-------+       +-------+       +-----------+
    """

    @classmethod
    @utils.requires_ext(extension='security-group', service='network')
    @utils.requires_ext(extension='tap-mirror-lport', service='network')
    def skip_checks(cls):
        super().skip_checks()
        if not (CONF.neutron_plugin_options.advanced_image_ref or
                CONF.neutron_plugin_options.default_image_is_advanced):
            raise cls.skipException(
                "Advanced image is required to run this test.")

    @classmethod
    def resource_setup(cls):
        super().resource_setup()
        tm_scenario._serialize_topology_lifetime(cls)
        cls.keypair = cls.create_keypair()
        cls.secgroup = cls.create_security_group(
            name=data_utils.rand_name('secgroup'))
        cls.create_loginable_secgroup_rule(secgroup_id=cls.secgroup['id'])
        cls.create_pingable_secgroup_rule(secgroup_id=cls.secgroup['id'])

        cls.setup_advanced_image()
        cls._taas_server_ids = []
        cls.network, cls.subnet, cls.router = (
            tm_scenario._create_shared_network_cls(cls))
        vm0, cls.vm0_port, vm0_fip = (
            tm_scenario._create_shared_server_with_floatingip_cls(
                cls, cls.network))
        _vm1, cls.vm1_port, _vm1_fip = (
            tm_scenario._create_shared_server_with_floatingip_cls(
                cls, cls.network))
        cls.vm0_client = tm_scenario._make_remote_client_cls(
            cls, vm0_fip, diagnostics=dict(
                label='vm0', server=vm0, port=cls.vm0_port,
                router=cls.router))
        cls.vm0_ip = cls.vm0_port['fixed_ips'][0]['ip_address']
        cls.vm1_ip = cls.vm1_port['fixed_ips'][0]['ip_address']

        netmon, _subnet, router = tm_scenario._create_shared_network_cls(cls)
        collector, cls.collector_port, collector_fip = (
            tm_scenario._create_shared_server_with_floatingip_cls(
                cls, netmon, use_taas_cloud_image=True,
                port_security_enabled=False))
        cls.collector_client = tm_scenario._make_remote_client_cls(
            cls, collector_fip, advanced=True, diagnostics=dict(
                label='collector', server=collector,
                port=cls.collector_port, router=router))
        cls.collector_ip = cls.collector_port['fixed_ips'][0]['ip_address']
        cls._set_collector_promiscuous()

    @classmethod
    def _set_collector_promiscuous(cls):
        """Let the collector receive frames addressed to other MACs.

        The copies keep the destination MAC of the mirrored traffic, and
        ``tcpdump -i any`` does not enable the promiscuous mode by itself.
        The instance is deleted with the class, so nothing is restored.
        """
        mac = cls.collector_port['mac_address'].lower()
        links = cls.collector_client.exec_command("ip -o link show")
        nics = [line.split(': ')[1].split('@')[0]
                for line in links.splitlines() if mac in line.lower()]
        if len(nics) != 1:
            raise RuntimeError(_("Can not find the interface of the "
                                 "collector port %(mac)s in:\n%(links)s") %
                               {'mac': mac, 'links': links})
        cls.collector_client.exec_command(
            "sudo ip link set dev %s promisc on" % nics[0])
        state = cls.collector_client.exec_command(
            "ip link show dev %s" % nics[0])
        if "PROMISC" not in state:
            raise RuntimeError(_("Interface %(nic)s of the collector is "
                                 "not promiscuous:\n%(state)s") %
                               {'nic': nics[0], 'state': state})

    @classmethod
    def resource_cleanup(cls):
        tm_scenario._cleanup_shared_servers(cls)
        super().resource_cleanup()

    def _create_lport_mirror(self, directions, port=None, **kwargs):
        port = port or self.vm1_port
        mirror = self.tap_mirrors_client.create_tap_mirror(
            name=data_utils.rand_name('tap_mirror_lport'),
            port_id=port['id'],
            directions=directions,
            remote_port_id=self.collector_port['id'],
            mirror_type=LPORT,
            **kwargs)['tap_mirror']
        self.addCleanup(test_utils.call_and_ignore_notfound_exc,
                        self.tap_mirrors_client.delete_tap_mirror,
                        mirror['id'])
        return mirror

    def _collector(self, cmd):
        return self.collector_client.exec_command(cmd)

    def _capture(self):
        """Ping vm1 from vm0 and count the copies seen on the collector.

        Returns ``(requests, replies)``: the number of echo requests
        (vm0 -> vm1) and echo replies (vm1 -> vm0) captured on the
        collector during one round of pings.

        A count of zero is only meaningful if the capture itself worked,
        so the capture has to report that it is listening before the pings
        start, has to see a control ping of the collector to itself, and
        has to terminate with its summary before the output is read.
        """
        self._collector("sudo pkill -x tcpdump || true")
        self._collector("sudo rm -f %s" % LOG_LOCATION)
        self._run_in_background(
            self.collector_client,
            "sudo tcpdump -l -n -i any icmp > %s" % LOG_LOCATION)
        common_utils.wait_until_true(
            lambda: "listening on" in self._collector(
                "cat %s || true" % LOG_LOCATION),
            timeout=CAPTURE_TIMEOUT, sleep=1,
            exception=RuntimeError("tcpdump did not start on the collector"))

        self.check_remote_connectivity(self.vm0_client, self.vm1_ip,
                                       ping_count=PING_COUNT)
        self._collector("ping -c 2 %s" % self.collector_ip)

        # SIGTERM makes tcpdump flush its output and print the summary.
        self._collector("sudo pkill -x tcpdump || true")
        common_utils.wait_until_true(
            lambda: "packets captured" in self._collector(
                "cat %s || true" % LOG_LOCATION),
            timeout=CAPTURE_TIMEOUT, sleep=1,
            exception=RuntimeError("tcpdump did not stop on the collector"))
        output = self._collector("cat %s" % LOG_LOCATION)
        LOG.debug("Capture on the collector:\n%s", output)

        control = output.count(
            "%s > %s: ICMP echo request" % (self.collector_ip,
                                            self.collector_ip))
        self.assertGreater(
            control, 0, "The capture missed the control ping, so it can "
            "not be trusted:\n%s" % output)
        requests = output.count(
            "%s > %s: ICMP echo request" % (self.vm0_ip, self.vm1_ip))
        replies = output.count(
            "%s > %s: ICMP echo reply" % (self.vm1_ip, self.vm0_ip))
        return requests, replies

    def _wait_for_capture(self, want_requests, want_replies):
        """Wait until one round of pings is captured as expected."""
        seen = {}

        def _as_expected():
            requests, replies = self._capture()
            seen['requests'], seen['replies'] = requests, replies
            return (bool(requests) == want_requests and
                    bool(replies) == want_replies)

        try:
            common_utils.wait_until_true(_as_expected,
                                         timeout=MIRROR_TIMEOUT, sleep=2)
        except common_utils.WaitTimeout:
            self.fail("Collector saw %(requests)d echo requests and "
                      "%(replies)d echo replies; expected requests: %(wreq)s,"
                      " replies: %(wrep)s" % dict(seen, wreq=want_requests,
                                                  wrep=want_replies))

    def _run_mirror_lifecycle(self, directions, want_requests, want_replies):
        # Without a mirror the collector must not see the conversation,
        # otherwise the checks below would prove nothing. Waiting also
        # covers the mirror of a previous test still being removed.
        self._wait_for_capture(False, False)

        mirror = self._create_lport_mirror(directions)
        self._wait_for_capture(want_requests, want_replies)

        self.tap_mirrors_client.delete_tap_mirror(mirror['id'])
        self._wait_for_capture(False, False)

    @decorators.idempotent_id('3b0d7f52-1c64-4d08-8a3e-0e5c2b9f6a10')
    def test_lport_mirror_api(self):
        """Create, show, list, update and delete an lport tap mirror."""
        mirror = self._create_lport_mirror({'BOTH': None})
        self.assertEqual(LPORT, mirror['mirror_type'])
        self.assertEqual(self.vm1_port['id'], mirror['port_id'])
        self.assertEqual(self.collector_port['id'], mirror['remote_port_id'])
        self.assertIsNone(mirror['remote_ip'])
        self.assertEqual({'BOTH': None}, mirror['directions'])

        shown = self.tap_mirrors_client.show_tap_mirror(
            mirror['id'])['tap_mirror']
        self.assertEqual(mirror, shown)

        listed = self.tap_mirrors_client.list_tap_mirrors(
            mirror_type=LPORT)['tap_mirrors']
        self.assertIn(mirror['id'], [m['id'] for m in listed])
        self.assertEqual({LPORT}, {m['mirror_type'] for m in listed})

        self.tap_mirrors_client.update_tap_mirror(
            mirror['id'], name='renamed', description='lport mirror')
        shown = self.tap_mirrors_client.show_tap_mirror(
            mirror['id'])['tap_mirror']
        self.assertEqual('renamed', shown['name'])
        self.assertEqual('lport mirror', shown['description'])
        self.assertEqual(self.collector_port['id'], shown['remote_port_id'])

        # One lport mirror per source port and direction.
        self.assertRaises(lib_exc.Conflict, self._create_lport_mirror,
                          {'IN': None})
        # Another source port can use the same collector.
        other = self._create_lport_mirror({'IN': None}, port=self.vm0_port)
        self.assertEqual(self.collector_port['id'], other['remote_port_id'])

        self.tap_mirrors_client.delete_tap_mirror(mirror['id'])
        self.assertRaises(lib_exc.NotFound,
                          self.tap_mirrors_client.show_tap_mirror,
                          mirror['id'])

    @decorators.idempotent_id('6f4a1c83-2d75-4e19-9b4f-1f6d3c0a7b21')
    def test_lport_mirror_both_directions(self):
        """Both the requests to vm1 and its replies reach the collector."""
        self._run_mirror_lifecycle({'BOTH': None},
                                   want_requests=True, want_replies=True)

    @decorators.idempotent_id('9a5b2d94-3e86-4f2a-ac50-2a7e4d1b8c32')
    def test_lport_mirror_in_direction(self):
        """IN mirrors the traffic going to vm1: only the echo requests."""
        self._run_mirror_lifecycle({'IN': None},
                                   want_requests=True, want_replies=False)

    @decorators.idempotent_id('c2d63ea5-4f97-403b-bd61-3b8f5e2c9d43')
    def test_lport_mirror_out_direction(self):
        """OUT mirrors the traffic leaving vm1: only the echo replies."""
        self._run_mirror_lifecycle({'OUT': None},
                                   want_requests=False, want_replies=True)
