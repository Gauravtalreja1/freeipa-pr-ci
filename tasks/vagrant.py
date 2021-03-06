import logging
import os
import re
import subprocess
import time
from datetime import datetime

import yaml

import tasks

from . import constants
from .common import FallibleTask, PopenTask, TaskException


def with_vagrant(func):
    def wrapper(self, *args, **kwargs):
        try:
            __setup_provision(self)
        except TaskException as exc:
            logging.critical('vagrant or provisioning failed')
            raise exc
        else:
            func(self, *args, **kwargs)
        finally:
            if not self.no_destroy:
                self.execute_subtask(
                    VagrantCleanup(raise_on_err=False))

    return wrapper


def __setup_provision(task):
    """
    This tries to execute the provision twice due to
    problems described in issue #20
    """
    task.execute_subtask(
        VagrantBoxDownload(
            box_name=task.template_name,
            box_version=task.template_version,
            link_image=task.link_image,
            timeout=None))
    try:
        task.execute_subtask(
            VagrantUp(action_name=task.action_name, timeout=None))
        task.execute_subtask(VagrantProvision(timeout=None))
    except Exception as exc:
        logging.debug(exc, exc_info=True)
        logging.info("Failed to provision/up VM. Trying it again")
        task.execute_subtask(VagrantCleanup(raise_on_err=False))
        task.execute_subtask(
            VagrantUp(action_name=task.action_name, timeout=None))
        task.execute_subtask(VagrantProvision(timeout=None))


class VagrantTask(FallibleTask):
    def __init__(self, **kwargs):
        super(VagrantTask, self).__init__(**kwargs)
        self.timeout = kwargs.get('timeout', None)


class VagrantUp(VagrantTask):
    def __init__(self, action_name, **kwargs):
        super(VagrantUp, self).__init__(**kwargs)
        self.action_name = action_name

    def _run(self):
        try:
            self.execute_subtask(
                PopenTask(['vagrant', 'up', '--no-provision', '--parallel'],
                          timeout=None))
        except Exception as exc:
            if self.action_name != 'ad':
                raise

            # Handle possible WinRM error: 'The device is not ready'
            logging.debug(exc, exc_info=True)
            logging.info("Retrying to bring the machine up.")
            # Trying again
            self.execute_subtask(
                PopenTask(['vagrant', 'up', '--no-provision', '--parallel'],
                          timeout=None))
            logging.info("Waiting before continuing to provision.")
            time.sleep(120)


class VagrantProvision(VagrantTask):
    def _run(self):
        self.execute_subtask(
            PopenTask(['vagrant', 'provision'],
                      timeout=None))


class VagrantCleanup(VagrantTask):
    def _run(self):
        logging.info("Destroying vagrant machines.")
        self.execute_subtask(
            PopenTask(["vagrant", "destroy", "--force"], raise_on_err=False))


class VagrantBoxDownload(VagrantTask):
    def __init__(self, box_name, box_version, link_image=True, **kwargs):
        """
        link_image: if True, a symbolic link will be created in libvirt to
                    conserve storage (otherwise, libvirt copies it by default)
        """
        super(VagrantBoxDownload, self).__init__(**kwargs)
        self.box = VagrantBox(box_name, box_version)
        self.link_image = True

    def _run(self):
        self.box.update_latest_use()

        if not self.box.exists():
            # If necessary delete oldest box to save space before downloading a
            # new one
            VagrantBox.delete_oldest_box()

            try:
                self.execute_subtask(
                    PopenTask([
                        'vagrant', 'box', 'add', self.box.name,
                        '--box-version', self.box.version,
                        '--provider', self.box.provider],
                        timeout=None))
            except TaskException as exc:
                logging.error('Box download failed')
                raise exc

        # link box to libvirt
        if self.link_image and not self.box.libvirt_exists():
            try:
                self.execute_subtask(
                    PopenTask([
                        'ln', self.box.vagrant_path, self.box.libvirt_path]))
                self.execute_subtask(
                    PopenTask([
                        'chown', 'qemu:qemu', self.box.libvirt_path]))
                self.execute_subtask(
                    PopenTask(['virsh', 'pool-refresh', 'default']))
            except TaskException as exc:
                logging.warning('Failed to create libvirt link to image')
                raise exc


class VagrantBox(object):
    def __init__(self, name, version, provider="libvirt"):
        self.name = name
        self.version = version
        self.provider = provider

    @property
    def escaped_name(self):
        return self.name.replace(
            '/', '-VAGRANTSLASH-')

    @property
    def vagrant_path(self):
        return constants.VAGRANT_IMAGE_PATH.format(
            name=self.escaped_name,
            version=self.version,
            provider=self.provider)

    @property
    def libvirt_name(self):
        return '{escaped_name}_vagrant_box_image'.format(
            escaped_name=self.escaped_name)

    @property
    def libvirt_path(self):
        return constants.LIBVIRT_IMAGE_PATH.format(
            libvirt_name=self.libvirt_name,
            version=self.version)

    @property
    def last_time_used(self):
        box_key = '{name}_{version}_{provider}'.format(
            name=self.name, version=self.version, provider=self.provider)
        with open(tasks.BOX_STATS_FILE, 'r') as stats_file:
            stats = yaml.safe_load(stats_file)
            if not stats:
                stats = {}

        return stats.get(box_key, None)

    @staticmethod
    def delete_oldest_box():
        all_boxes = sorted(
            VagrantBox.installed_boxes(), key=lambda x: x.last_time_used)

        # Do not delete Windows boxes
        linux_boxes = [x for x in all_boxes if 'windows' not in x.name]
        if len(linux_boxes) > 4:
            linux_boxes[0].delete_box()

    @staticmethod
    def installed_boxes():
        output = subprocess.check_output(
            ['vagrant', 'box', 'list'], timeout=2000)

        if 'There are no installed boxes!' in output.decode():
            return []

        all_boxes = []
        for box_data in output.decode().strip().split('\n'):
            matches = re.search(
                r'(?P<name>[\/\w-]+)\s+\((?P<provider>[\w-]+)\,\s(?P<version>[\w.]+)\)',  # noqa
                output.decode(),
            )
            box = VagrantBox(
                name=matches.group('name'),
                version=matches.group('version'),
                provider=matches.group('provider'),
            )
            all_boxes.append(box)

        return all_boxes

    def update_latest_use(self):
        box_key = '{name}_{version}_{provider}'.format(
            name=self.name, version=self.version, provider=self.provider)
        with open(tasks.BOX_STATS_FILE, 'r+') as stats_file:
            stats = yaml.safe_load(stats_file)
            if not stats:
                stats = {}

        stats[box_key] = datetime.now()
        with open(tasks.BOX_STATS_FILE, 'w+') as stats_file:
            yaml.dump(stats, stats_file)

    def exists(self):
        return os.path.exists(self.vagrant_path)

    def libvirt_exists(self):
        return os.path.exists(self.libvirt_path)

    def delete_box(self):
        subprocess.run([
            'vagrant', 'box', 'remove', self.name, '--provider', self.provider,
            '--box-version', self.version
        ], timeout=2000)

        subprocess.run(
            ['virsh', 'vol-delete', self.libvirt_path], timeout=2000)
