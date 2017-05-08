#!/usr/bin/env python
# -*- coding: utf-8 -*-

# Must set /usr/config/config.xml NetworkKey

import datetime
import json
import logging
import os
import sys
import time
import yaml

import openzwave
from openzwave.controller import ZWaveController
from openzwave.network import ZWaveNetwork
from openzwave.option import ZWaveOption
from pydispatch import dispatcher
import paho.mqtt.client as paho

#logging.getLogger('openzwave').addHandler(logging.NullHandler())
#logging.basicConfig(level=logging.DEBUG)
logging.basicConfig(level=logging.INFO, format='%(asctime)s %(name)-10s %(message)s')
logger = logging.getLogger('openzwave')

device = "/dev/ttyACM0"
log = "None"

for arg in sys.argv:
    if arg.startswith("--device"):
        temp, device = arg.split("=")
    elif arg.startswith("--log"):
        temp, log = arg.split("=")
    elif arg.startswith("--help"):
        print("help : ")
        print("  --device=/dev/yourdevice ")
        print("  --log=Info|Debug")

# Define some manager options
import python_openzwave
config_path = os.path.join(os.path.dirname(python_openzwave.__file__), 'ozw_config')
options = ZWaveOption(device, \
  config_path=config_path, \
  user_path=".", cmd_line="")
options.set_log_file("OZW_Log.log")
options.set_append_log_file(False)
options.set_console_output(False)
options.set_save_log_level(log)
options.set_logging(True)
options.lock()

LOCK_ALARM_TYPE = {
    9: 'Deadbolt Jammed',
    18: 'Locked with Keypad by user ',
    19: 'Unlocked with Keypad by user ',
    21: 'Manually Locked ',
    22: 'Manually Unlocked ',
    24: 'Locked by RF',
    25: 'Unlocked by RF',
    27: 'Auto re-lock',
    33: 'User deleted: ',
    112: 'Master code changed or User added: ',
    113: 'Duplicate Pin-code: ',
    130: 'RF module, power restored',
    161: 'Tamper Alarm: ',
    167: 'Low Battery',
    168: 'Critical Battery Level',
    169: 'Battery too low to operate'
}

UNLOCKED_ALARM_TYPES = set([19, 22, 25])
LOCKED_ALARM_TYPES = set([18, 21, 24, 27])

class Main(object):
    config = None
    device_to_node = {}
    node_to_device = {}

    def network_started(self, network):
        logger.info("network started: homeid {:08x} - {} nodes were found.".format(network.home_id, network.nodes_count))

    def network_failed(self, network):
        logger.info("network failed")

    def network_ready(self, network):
        logger.info("network ready: %d nodes were found", network.nodes_count)
        logger.info("network ready: controller is %s", network.controller)

        # connect to updates after initialization has finished
        dispatcher.connect(self.node_update, ZWaveNetwork.SIGNAL_NODE)
        dispatcher.connect(self.value_update, ZWaveNetwork.SIGNAL_VALUE)
        dispatcher.connect(self.ctrl_message, ZWaveController.SIGNAL_CONTROLLER)

    def node_update(self, network, node):
        logger.info("node update: %s", node)

    def value_update(self, network, node, value):
        # logger.info("value update: %s", value)
        device = self.node_to_device.get(node.node_id)
        if not device:
            return

        if value.label == 'Alarm Type':
            logger.info("Alarm: %s", LOCK_ALARM_TYPE.get(value.data))

            if value.data in UNLOCKED_ALARM_TYPES:
                self.pub_device_state(device, True)
            elif value.data in LOCKED_ALARM_TYPES:
                self.pub_device_state(device, False)
        elif value.label == 'Switch':
            logger.info("Switch: %s", value.data)
            self.pub_device_state(device, value.data)

    def pub_device_state(self, device, on):
        timestamp = datetime.datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S.%f')
        message = {
            'topic': 'openzwave',
            'timestamp': timestamp,
            'device': device,
            'command': 'on' if on else 'off',
        }
        message = json.dumps(message)
        self.client.publish('gohome/openzwave', message)

    def set_device_state(self, node_id, on):
        node = self.network.nodes.get(node_id)
        if not node:
            logger.warn('No node %d found', node_id)
            return
        by_label = {val.label: val for val in node.values.values()}

        if 'COMMAND_CLASS_DOOR_LOCK' in node.command_classes_as_string:
            logger.info('Unlocking...' if on else 'Locking...')
            by_label['Locked'].data = not on
            logger.info("Locked set to %s", not on)
        elif 'COMMAND_CLASS_SWITCH_BINARY' in node.command_classes_as_string:
            logger.info('Switching on...' if on else 'Switching off...')
            by_label['Switch'].data = on
            logger.info("Switch set to %s", on)
        else:
            logger.info("Node %d not in recognised classes", node_id)

    def ctrl_message(self, state, message, network, controller):
        logger.info('controller message: %s', message)

    def lock_node(self):
        def nodes_matching_class(name):
            return filter(lambda n: name in n.command_classes_as_string, self.network.nodes.values())
        return next(nodes_matching_class('COMMAND_CLASS_DOOR_LOCK'), None)

    def on_mqtt_connect(self, client, userdata, flags, rc):
        logger.info('Connected to MQTT')
        client.subscribe('gohome/command/#')
        client.subscribe('gohome/config')

    def on_mqtt_message(self, client, userdata, msg):
        message = json.loads(msg.payload)
        topic = message['topic']
        if topic == 'config':
            self.config = yaml.load(message['config'])
            self.node_to_device = self.config['protocols']['zwave']
            self.device_to_node = {
                device: node_id
                for node_id, device in self.node_to_device.items()
            }

        elif topic == 'command':
            if message['device'] not in self.device_to_node:
                return

            logger.info('Command received: %s', msg.payload)
            node_id = self.device_to_node[message['device']]
            on = message['command'] == 'on'
            self.set_device_state(node_id, on)

    def run(self):
        # Connect to mqtt
        client = paho.Client()
        client.on_connect = self.on_mqtt_connect
        client.on_message = self.on_mqtt_message
        client.connect('localhost')
        self.client = client

        # Create a network object
        self.network = ZWaveNetwork(options, autostart=False)

        dispatcher.connect(self.network_started, ZWaveNetwork.SIGNAL_NETWORK_STARTED)
        dispatcher.connect(self.network_failed, ZWaveNetwork.SIGNAL_NETWORK_FAILED)
        dispatcher.connect(self.network_ready, ZWaveNetwork.SIGNAL_NETWORK_READY)

        self.network.start()

        client.loop_forever()

if __name__ == '__main__':
    Main().run()
