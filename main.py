#!/usr/bin/env python
# -*- coding: utf-8 -*-

# Must set /usr/config/config.xml NetworkKey

import collections
import datetime
import json
import logging
import os
import re
import signal
import sys
import threading
import yaml

from openzwave.controller import ZWaveController
from openzwave.network import ZWaveNetwork
from openzwave.option import ZWaveOption
from pydispatch import dispatcher
import python_openzwave
import paho.mqtt.client as paho

# logging.getLogger('openzwave').addHandler(logging.NullHandler())
# logging.basicConfig(level=logging.DEBUG)
logging.basicConfig(level=logging.INFO, format='[%(name)19s] %(message)s')
default_logger = logging.getLogger('main')

device = '/dev/ttyACM0'
log = 'None'

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
config_path = os.path.join(os.path.dirname(python_openzwave.__file__), 'ozw_config')
options = ZWaveOption(device, config_path=config_path, user_path='.', cmd_line='')
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

# on = locked, off = unlocked
LOCK_ALARM_STATE = {
    9:  'jammed',
    18: 'off',
    19: 'on',
    21: 'off',
    22: 'on',
    24: 'off',
    25: 'on',
    27: 'off',
    167: 'battery',
    168: 'battery',
    169: 'battery',
}

ACCESS_CONTROL = {
    22: 'Open',
    23: 'Closed',
}

ACCESS_CONTROL_STATE = {
    22: 'on',
    23: 'off',
}

BURGLAR = {
    3: 'Removed from wall',
    8: 'Motion',
}

class Main(object):
    config = None
    device_to_node = {}
    node_to_device = {}
    node_to_logger = collections.defaultdict(lambda: default_logger)
    node_ready = {}
    timers = {}

    def network_started(self, network):
        default_logger.info("network started")
        dispatcher.connect(self.node_queries_complete, ZWaveNetwork.SIGNAL_NODE_QUERIES_COMPLETE)

    def network_failed(self, network):
        default_logger.info("network failed")

    def node_queries_complete(self, network, node):
        logger = self.node_to_logger[node.node_id]
        logger.info("node %d queries complete: %s %s",
                    node.node_id, node.product_name, node.manufacturer_name)
        logger.info("- command classes: %s", node.command_classes_as_string)
        logger.info("- capabilities: %s", node.capabilities)
        logger.info("- neighbors: %s", node.neighbors)
        self.node_ready[node.node_id] = True

    def network_ready(self, network):
        default_logger.info("network ready: %d nodes were found", network.nodes_count)
        # connect to updates after initialization has finished
        dispatcher.connect(self.value_update, ZWaveNetwork.SIGNAL_VALUE)
        dispatcher.connect(self.node_update, ZWaveNetwork.SIGNAL_NODE)
        dispatcher.connect(self.node_event, ZWaveNetwork.SIGNAL_NODE_EVENT)
        dispatcher.connect(self.ctrl_message, ZWaveController.SIGNAL_CONTROLLER)

    def node_update(self, network, node):
        logger = self.node_to_logger[node.node_id]
        logger.info("node update: %s", node)

    def node_event(self, network, node, value):
        device = self.node_to_device.get(node.node_id)
        logger = self.node_to_logger[node.node_id]
        logger.info("node event: value: %s", value)
        if not device:
            return
        self.value_basic(logger, node, device, value)

    def value_update(self, network, node, value):
        device = self.node_to_device.get(node.node_id)
        logger = self.node_to_logger[node.node_id]
        logger.info("value update: %s=%s", value.label, value.data_as_string)
        if not device:
            return
        timer = self.timers.pop(device, None)
        if timer:
            timer.cancel()

        fn = getattr(self, 'value_%s' % value.label.replace(' ', '_'), None)
        if fn:
            fn(logger, node, device, value)

    def value_basic(self, logger, node, device, value):
            state = 'on' if value == 255 else 'off'
            logger.info('Basic sensor update: %s', state)
            if state is not None:
                self.pub_device_state(device, state, 'sensor')

    def value_Alarm_Type(self, logger, node, device, value):
        if 'COMMAND_CLASS_DOOR_LOCK' in node.command_classes_as_string:
            if value.data not in LOCK_ALARM_TYPE:
                logger.warn('Lock update unknown: %s', value.data)
                return
            logger.info('Lock update: %s', LOCK_ALARM_TYPE[value.data])
            state = LOCK_ALARM_STATE.get(value.data)
            if state is not None:
                self.pub_device_state(device, state, 'lock')

    def value_Switch(self, logger, node, device, value):
        state = 'on' if value.data else 'off'
        logger.info('Switch update: %s', state)
        self.pub_device_state(device, state, 'ack')

    def value_Sensor(self, logger, node, device, value):
        # Neo CoolCam Door/Window sensors emit both Sensor and Access Control
        # for events, but use both for reliability.
        state = 'on' if value.data else 'off'
        logger.info('Sensor update: %s', state)
        self.pub_device_state(device, state, 'sensor')

    def value_Access_Control(self, logger, node, device, value):
        # Philio 4 in 1 Multi-Sensor only emits this for open/close.
        state = ACCESS_CONTROL_STATE.get(value.data)
        if state is None:
            logger.warn('Access control unknown: %s', value.data)
            return
        logger.info('Access control update: %s', state)
        self.pub_device_state(device, state, 'sensor')

    def value_Temperature(self, logger, node, device, value):
        if value.units == 'F':
            celsius = (value.data - 32) * 5/9
        else:
            celsius = value.data
        logger.debug('Temperature: %.1fC', celsius)
        device = 'temp.' + device.split('.')[-1]
        message = {
            'topic': 'temp',
            'device': device,
            'temp': celsius,
        }
        self.publish(message)

    def value_Luminance(self, logger, node, device, value):
        # label: [Luminance] data: [16.0]
        device = 'lux.' + device.split('.')[-1]
        message = {
            'topic': 'lux',
            'device': device,
            'lux': value.data,
        }
        self.publish(message)

    def value_Battery_Level(self, logger, node, device, value):
        # label: [Battery Level] data: [100]
        message = {
            'topic': 'openzwave',
            'device': device,
            'battery': value.data,
        }
        self.publish(message)

    def value_Burglar(self, logger, node, device, value):
        state = BURGLAR.get(value.data)
        if state is None:
            logger.warn("Burglar unknown: %s", value.data)
            return

        logger.info("motion update: %s", state)
        if state == 'Motion':
            device = 'pir.' + device.split('.')[-1]
            self.pub_device_state(device, 'on', 'pir')

            # sensors do not send off, so trigger this on a timer delay
            if device in self.timers:
                self.timers[device].cancel()

            def switch_off():
                logger.info("%s motion auto off", device)
                self.pub_device_state(device, 'off', 'pir')
            timer = self.timers[device] = threading.Timer(60.0, switch_off)
            timer.start()

    def pub_device_state(self, device, command, topic):
        message = {
            'topic': topic,
            'device': device,
            'command': command,
        }
        self.publish(message)

    def publish(self, message):
        topic = 'gohome/%s/%s' % (message['topic'], message['device'])
        message['timestamp'] = datetime.datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S.%f')[:-3]
        message = json.dumps(message)
        self.client.publish(topic, message)

    def set_device_state(self, node_id, on):
        node = self.network.nodes.get(node_id)
        logger = self.node_to_logger[node_id]
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
        default_logger.info('controller message: %s', message)

    def lock_node(self):
        def nodes_matching_class(name):
            return filter(
                lambda n: name in n.command_classes_as_string,
                self.network.nodes.values()
            )
        return next(nodes_matching_class('COMMAND_CLASS_DOOR_LOCK'), None)

    def on_mqtt_connect(self, client, userdata, flags, rc):
        default_logger.info('Connected to MQTT')
        client.subscribe('gohome/command/#')
        client.subscribe('gohome/config')

    def on_mqtt_message(self, client, userdata, msg):
        if msg.payload.startswith(b'---'):
            message = yaml.safe_load(msg.payload)
        else:
            message = json.loads(msg.payload)
        if 'topic' in message:
            topic = message['topic']
        else:
            topic = msg.topic.split('/')[1]
        if topic == 'config':
            self.config = message
            self.node_to_device = {
                int(device['source'][6:]): _id
                for _id, device in self.config['devices'].items()
                if 'source' in device and device['source'].startswith('zwave.')
            }
            self.device_to_node = {
                device: node_id
                for node_id, device in self.node_to_device.items()
            }
            default_logger.info(str(self.node_to_device))
            self.node_to_logger = collections.defaultdict(lambda: default_logger)
            for node_id, device in self.node_to_device.items():
                self.node_to_logger[node_id] = logging.getLogger(device)
            default_logger.info('Configured devices')

        elif topic == 'command':
            if message['device'] not in self.device_to_node:
                return

            default_logger.info('Command received: %s', msg.payload)
            device = message['device']
            node_id = self.device_to_node[device]
            on = message['command'] == 'on'
            self.set_device_state(node_id, on)

            def repeat():
                self.set_device_state(node_id, on)
            timer = self.timers[device] = threading.Timer(5.0, repeat)
            timer.start()

    def setup_mqtt(self):
        self.client = paho.Client()
        self.client.on_connect = self.on_mqtt_connect
        self.client.on_message = self.on_mqtt_message
        url = os.getenv('GOHOME_MQTT')
        if not url:
            default_logger.error("Please set GOHOME_MQTT")
            sys.exit()
        m = re.search(r'^tcp://([^:]+)(?::(\d+))?$', url)
        if not m:
            default_logger.error("Invalid value for GOHOME_MQTT: %s", url)
            sys.exit()
        hostname, port = m.groups()
        port = int(port) if port else 1883
        default_logger.info('Connecting to mqtt server: %s:%d', hostname, port)
        self.client.connect(hostname, port=port)

    def run(self):
        # Connect to mqtt
        self.setup_mqtt()

        # Create a network object
        self.network = ZWaveNetwork(options, autostart=False)

        # Hook Ctrl-C to cleanly shutdown.
        # This ensures openzwave persists its state to the zwcfg xml file.
        def signal_handler(signal, frame):
            default_logger.info("Stopping zwave network")
            self.network.stop()
            default_logger.info("Stopping mqtt client")
            self.client.disconnect()

        signal.signal(signal.SIGINT, signal_handler)
        signal.signal(signal.SIGTERM, signal_handler)

        dispatcher.connect(self.network_started, ZWaveNetwork.SIGNAL_NETWORK_STARTED)
        dispatcher.connect(self.network_failed, ZWaveNetwork.SIGNAL_NETWORK_FAILED)
        dispatcher.connect(self.network_ready, ZWaveNetwork.SIGNAL_AWAKE_NODES_QUERIED)

        self.network.start()

        self.client.loop_forever()
        default_logger.info("Finished")

if __name__ == '__main__':
    Main().run()
