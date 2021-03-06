import argparse
import collections
import copy
import os
import platform
import shlex
import yaml

import rospkg
from roslaunch.config import ROSLaunchConfig
from roslaunch.xmlloader import XmlLoader, ifunless
from systemd_ros.config_parser import SystemdConfigParser

PARAM_MANAGER = ('systemd_ros', 'param-manager')
ROS_ROOT = os.path.dirname(rospkg.get_ros_paths()[-1])
ENV_SH = os.path.join(ROS_ROOT, 'env.sh')
REMOTE_ENV_LOADER = ('systemd_ros', 'remote-env.sh')


class FastXmlLoader(XmlLoader):

    @ifunless
    def _param_tag(self, *args, **kwargs):
        pass

    @ifunless
    def _rosparam_tag(self, *arsg, **kwargs):
        pass


class ServiceGenerator(object):

    def __init__(self, main_service_name,
                 launch_config, launch_file_name,
                 extra_config, user, group):
        self.main_service_name = main_service_name
        self.launch_config = launch_config
        self.launch_file_name = launch_file_name
        self.extra_config = extra_config
        self.user = user
        self.group = group
        pkg_path = rospkg.RosPack().get_path('systemd_ros')
        self.local_env_args = {
            'ROS_PYTHON_LOG_CONFIG_FILE':
                os.path.join(pkg_path, 'systemd_logging_without_rosout.conf'),
            'ROSCONSOLE_CONFIG_FILE':
                os.path.join(pkg_path, 'systemd_log4cxx_no_output.conf'),
            'HOSTNAME': platform.node(),
        }.items()

    @property
    def master_uri(self):
        return self.launch_config.master.uri

    def get_machine(self, node):
        return self.launch_config.machines[node.machine_name or '']

    def node_name_to_service(self, node_name):
        return '{}-{}.service'.format(
            self.main_service_name.rstrip('.service'),
            node_name.lstrip('/').replace('/', '_'))

    def generate_node_config(self, node):
        full_name = node.namespace + node.name
        machine = self.get_machine(node)

        data = {
            'Unit': {
                'Description': full_name,
            },
            'Service': {
                'User': self.user,
                'Group': self.group,
                'StandardOutput': 'journal',
                'StandardError': 'journal',
                'SyslogIdentifier': full_name,
                'KillSignal': 'SIGINT',
                'TimeoutStopSec': '10',
            },
            'Install': {
                'WantedBy': self.main_service_name,
            },
        }
        node_args = shlex.split(node.args)
        if node.package == node.type == 'nodelet' and node_args[0] == 'load':
            manager_name = node_args[2]
            if not manager_name.startswith('/'):
                manager_name = node.namespace + manager_name
            manager_service = self.node_name_to_service(manager_name)
            data['Unit'].update({
                'After': 'roscore.service {}'.format(manager_service),
                'BindsTo': 'roscore.service {}'.format(manager_service)
            })
        else:
            data['Unit'].update({
                'After': 'roscore.service {}'.format(self.main_service_name),
                'BindsTo': 'roscore.service {}'.format(self.main_service_name)
            })

        if node.respawn:
            data['Service']['Restart'] = 'always'
            if node.respawn_delay:
                data['Service']['RestartSec'] = "{0:.0f}s".format(
                    node.respawn_delay)

        if machine.address in ('localhost', '127.0.0.1', '127.0.1.1'):
            env_sh = ENV_SH
            data['Service']['Environment'] = ' '.join(
                '"{}={}"'.format(*kv)
                for kv in node.env_args + list(self.local_env_args))
        else:
            data['Service']['SuccessExitStatus'] = 130
            data['Service']['Environment'] = (
                '"ROS_MASTER_URI={}"'.format(self.master_uri)
            )
            data['Service']['ExecStop'] = (
                '{env_sh} rosnode kill {full_name}'
                ''.format(
                    env_sh=ENV_SH,
                    full_name=full_name,
                )
            )
            env_sh = (
                '/usr/bin/ssh -p {port}{user} '
                '-o ConnectTimeout={timeout} '
                '-o ServerAliveInterval=10 {address} '
                '{env_loader} rosrun {remote_env_loader}'
                ''.format(
                    port=machine.ssh_port,
                    user=' -l {}'.format(machine.user) if machine.user else '',
                    timeout=int(machine.timeout),
                    address=machine.address,
                    env_loader=machine.env_loader,
                    remote_env_loader=' '.join(REMOTE_ENV_LOADER)
                ))

        data['Service']['ExecStart'] = (
            '{env_sh} rosrun{prefix} {package} {type} '
            '__master:={master} __ns:={namespace} __name:={name} '
            '{remappings} {args}'
            ''.format(
                env_sh=env_sh,
                prefix=(' --prefix="{}"'.format(node.launch_prefix)
                        if node.launch_prefix and 'delayed' not in node.launch_prefix else ''),
                package=node.package,
                type=node.type,
                args=node.args,
                master=self.master_uri,
                namespace=node.namespace,
                name=node.name,
                remappings=' '.join("{}:={}".format(*kv)
                                    for kv in node.remap_args),
            ))

        self.mixin_extra_config(data, full_name)

        return (self.node_name_to_service(full_name),
                SystemdConfigParser.from_dict(data))

    def mixin_extra_config(self, data, full_name):
        for section, items in self.extra_config.get(full_name, {}).items():
            if section not in data:
                data[section] = {}
            for key, value in items.items():
                if key in data[section]:
                    data[section][key] += ' ' + value
                else:
                    data[section][key] = value

    def generate_main_node_config(self):
        main_node_name = self.main_service_name\
            .rstrip('.service').replace('-', '_')

        return SystemdConfigParser.from_dict({
            'Unit': {
                'Description': self.main_service_name,
                'After': 'roscore.service',
                'BindsTo': 'roscore.service',
            },
            'Service': {
                'Type': 'notify',
                'User': self.user,
                'Group': self.group,
                'StandardOutput': 'journal',
                'StandardError': 'journal',
                'SyslogIdentifier': '/' + main_node_name,
                'Environment': ' '.join('"{}={}"'.format(*kv)
                                        for kv in self.local_env_args),
                'ExecStart': ('{env_sh} rosrun '
                              '{param_manager_pkg} {param_manager_node} '
                              '__master:={master} __name:={name} {launch_file}'
                              ''.format(
                                  env_sh=ENV_SH,
                                  param_manager_pkg=PARAM_MANAGER[0],
                                  param_manager_node=PARAM_MANAGER[1],
                                  master=self.master_uri,
                                  name=main_node_name,
                                  launch_file=self.launch_file_name)),
                'ExecReload': '/bin/kill -HUP $MAINPID',
                'KillSignal': 'SIGINT',
                'TimeoutStartSec': '10800',
                'TimeoutStopSec': '15',
            },
            'Install': {
                'WantedBy': 'multi-user.target'
            }
        })

    def generate_manager_and_nodelets(self, manager, nodelets):
        node = copy.deepcopy(manager)
        node.package = 'systemd_ros'
        node.type = 'nodelet_manager'
        args = []
        for nodelet in nodelets:
            nodelet_args = shlex.split(nodelet.args)
            args.append(nodelet_args[1])
            args.append('{}{}'.format(nodelet.namespace, nodelet.name))
            for kv in nodelet.remap_args:
                args.append('{}:={}'.format(*kv))
        node.args = '\\\n '.join(args)

        return self.generate_node_config(node)

    def generate_services(self, output_dir):

        if not os.path.exists(output_dir):
            os.makedirs(output_dir)

        # links and folderes have to be created manually here since this
        # is a systemd.generator (see manpage)
        wants_dir = os.path.join(
            output_dir,
            self.main_service_name + '.wants')
        if not os.path.exists(wants_dir):
            os.makedirs(wants_dir)

        service_configs = {
            self.main_service_name: self.generate_main_node_config()
        }

        nodelets = collections.defaultdict(list)
        managers = {}
        standard_nodes = []

        for node in self.launch_config.nodes:
            node_args = shlex.split(node.args)
            if node.package == node.type == 'nodelet' and node_args:
                if node_args[0] == 'load':
                    nodelets[node_args[2]].append(node)
                elif node_args[0] == 'manager':
                    managers[node.name] = node
                else:
                    standard_nodes.append(node)
            else:
                standard_nodes.append(node)

        for node in standard_nodes:
            service_configs.setdefault(*self.generate_node_config(node))

        for manager, nodelets_ in nodelets.items():
            if manager in managers:
                service_configs.setdefault(*self.generate_manager_and_nodelets(
                    managers[manager], nodelets_))
            else:
                # nodelet which loads into an unknown manager
                service_configs.setdefault(*self.generate_node_config(node))

        for manager in set(managers.keys()) - set(nodelets.keys()):
            # managers without nodelets
            service_configs.setdefault(*self.generate_node_config(
                managers[manager]))

        for service_name, conf in service_configs.items():
            service_path = os.path.join(output_dir, service_name)
            with open(service_path, 'w') as handle:
                conf.write(handle)

            if service_name != self.main_service_name:
                wants_file = os.path.join(wants_dir, service_name)
                if not os.path.exists(wants_file):
                    os.symlink(os.path.join(os.pardir, service_name),
                               wants_file)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('main-service', type=str)
    parser.add_argument('launch-file', type=argparse.FileType('r'))
    parser.add_argument('output-dir', type=str)
    parser.add_argument('--extra-config', type=argparse.FileType('r'))
    parser.add_argument('--user', type=str, default='ros')
    parser.add_argument('--group', type=str, default='ros')
    parser.add_argument('--ros-arg', action='append', dest='ros_args')
    parser.set_defaults(ros_args=[])
    args = parser.parse_args()

    launch_file_name = os.path.abspath(getattr(args, 'launch-file').name)

    # Close the file descriptor
    getattr(args, 'launch-file').close()

    config = ROSLaunchConfig()
    FastXmlLoader().load(launch_file_name, config, verbose=False,
                         argv=args.ros_args)

    extra_config = {}
    if args.extra_config:
        extra_config = yaml.safe_load(args.extra_config)
        args.extra_config.close()

    main_service_name = getattr(args, 'main-service')
    if not main_service_name.endswith('.service'):
        main_service_name += '.service'

    generator = ServiceGenerator(
        main_service_name, config, launch_file_name,
        extra_config, args.user, args.group)
    generator.generate_services(getattr(args, 'output-dir'))
