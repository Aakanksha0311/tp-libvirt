import logging
import shutil

import aexpect
from avocado.utils import process
from virttest import libvirt_version
from virttest import remote
from virttest import utils_misc
from virttest import utils_net
from virttest import utils_package
from virttest import utils_selinux
from virttest import virsh
from virttest.libvirt_xml import vm_xml
from virttest.staging import service
from virttest.utils_libvirt import libvirt_vmxml
from virttest.utils_test import libvirt

from provider.virtual_network import passt

LOG = logging.getLogger('avocado.' + __name__)


def run(test, params, env):
    """
    Test negative settings of passt backend interface
    """
    libvirt_version.is_libvirt_feature_supported(params)
    root = 'root_user' == params.get('user_type', '')
    virsh_uri = params.get('virsh_uri')
    if root:
        vm_name = params.get('main_vm')
        virsh_ins = virsh
        log_dir = params.get('log_dir')
        user_id = params.get('user_id')
    else:
        vm_name = params.get('unpr_vm_name')
        test_user = params.get('test_user', '')
        test_passwd = params.get('test_passwd', '')
        user_id = passt.get_user_id(test_user)
        host_session = aexpect.ShellSession('su')
        remote.VMManager.set_ssh_auth(host_session, 'localhost', test_user,
                                      test_passwd)
        host_session.close()
        virsh_ins = virsh.VirshPersistent(uri=virsh_uri)

    scenario = params.get('scenario')
    operation = params.get('operation')
    status_error = 'yes' == params.get('status_error')
    error_msg = params.get('error_msg')
    iface_attrs = eval(params.get('iface_attrs'))
    host_iface = params.get('host_iface')
    host_iface = host_iface if host_iface else utils_net.get_net_if(
        state="UP")[0]
    log_file = f'/run/user/{user_id}/passt.log' \
        if not params.get('log_file') else params['log_file']
    iface_attrs['backend']['logFile'] = log_file
    iface_attrs['source']['dev'] = host_iface

    vmxml = vm_xml.VMXML.new_from_inactive_dumpxml(vm_name,
                                                   virsh_instance=virsh_ins)
    bkxml = vmxml.copy()

    selinux_status = passt.ensure_selinux_enforcing()
    passt.check_socat_installed()

    firewalld = service.Factory.create_service("firewalld")
    try:
        if root:
            passt.make_log_dir(user_id, log_dir)

        host_iface_list = utils_net.get_linux_iface_info()

        if scenario == 'rm_passt_pgk':
            utils_package.package_remove('passt')
        elif scenario == 'inactive_host_iface':
            inactive_iface_list = [iface for iface in host_iface_list
                                   if iface['operstate'] == 'DOWN']
            if not inactive_iface_list:
                test.error('No inactive interface on host')
            host_iface = inactive_iface_list[0]['ifname']
            iface_attrs['source']['dev'] = host_iface
        elif scenario == 'non_exist_bind_ip':
            bind_ip = ''

            def _get_test_ip():
                nonlocal bind_ip
                bind_ip = passt.generate_random_ip_addr()
                if bind_ip not in str(host_iface_list):
                    return True
            if not utils_misc.wait_for(_get_test_ip, 10, 0.1):
                test.error('Cannot generate test ip not exists on host')

            portForwards = eval(params.get('portForwards').replace(
                'IP_EXAMPLE', bind_ip))
            iface_attrs.update(portForwards)
        elif scenario == 'port_occupied':
            port = passt.get_free_port()
            portForwards = eval(params.get('portForwards').replace(
                'PORT_EXAMPLE', str(port)))
            iface_attrs.update(portForwards)
            host_session = aexpect.ShellSession('su')
            host_session.sendline(f'socat TCP6-LISTEN:{port} -')
            process.run(f'ss -tlnp|grep {port}',
                        shell=True, ignore_status=True)
        elif scenario == 'port_under_1024':
            for port in range(1000, 0, -1):
                if process.run(f'ss -tlnp|grep {port}',  shell=True,
                               ignore_status=True).exit_status != 0:
                    portForwards = eval(params.get('portForwards').replace(
                        'PORT_EXAMPLE', str(port)))
                    iface_attrs.update(portForwards)
                    break
        else:
            return

        vmxml.del_device('interface', by_tag=True)
        iface_device = libvirt_vmxml.create_vm_device_by_type('interface',
                                                              iface_attrs)
        LOG.debug(f'Interface xml:\n{iface_device}')

        if operation == 'start_vm':
            vmxml.add_device(iface_device)
            vmxml.sync(virsh_instance=virsh_ins)
            result = virsh.start(vm_name, uri=virsh_uri, debug=True)
        if operation == 'hotplug':
            virsh.start(vm_name, uri=virsh_uri)
            result = virsh.attach_device(vm_name, iface_device.xml,
                                         uri=virsh_uri, debug=True)

        libvirt.check_exit_status(result, status_error)
        if error_msg:
            libvirt.check_result(result, error_msg)

    finally:
        if scenario == 'rm_passt_pgk':
            utils_package.package_install('passt')
        firewalld.start()
        virsh.destroy(vm_name, uri=virsh_uri)
        bkxml.sync(virsh_instance=virsh_ins)
        if root:
            shutil.rmtree(log_dir)
        else:
            del virsh_ins
        utils_selinux.set_status(selinux_status)
