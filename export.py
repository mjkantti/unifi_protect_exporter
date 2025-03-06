#!/usr/bin/python3 -u
#coding: utf8

import requests
import logging
import configparser
import sys
import traceback

from sched import scheduler
from time import time
from prometheus_client import start_http_server
from prometheus_client.core import GaugeMetricFamily, CounterMetricFamily, REGISTRY

class NVRCollector(object):
    def __init__(self, conf):
        self.conf = conf
        self.stats_cache = {}
        self.session = requests.Session()
        self.ts = 0

        # Metrics
        nvr_common_label_names = ['id', 'name', 'host', 'mac']
        cam_common_label_names = ['name', 'host', 'cameraName', 'cameraHost', 'cameraMac']

        self.metrics = {}
        self.metrics['cpu_load'] = GaugeMetricFamily('unvr_cpu_load', 'CPU Average Load', labels=nvr_common_label_names)
        self.metrics['cpu_temperature'] = GaugeMetricFamily('unvr_cpu_temperature', 'CPU Temperature', labels=nvr_common_label_names)

        self.metrics['hdd_state'] = GaugeMetricFamily('unvr_hard_drive_state', 'NVR Hard Drive State', labels=nvr_common_label_names + ['hard_disk_state'])

        hdd_label_names = nvr_common_label_names + ['hdd_slot', 'hdd_model', 'hdd_health', 'hdd_state']
        self.metrics['hdd_health'] = GaugeMetricFamily('unvr_hard_disk_health', 'NVR Hard Disk Health', labels=hdd_label_names)
        self.metrics['hdd_size'] = GaugeMetricFamily('unvr_hard_disk_size', 'NVR Hard Disk Size', labels=hdd_label_names)
        self.metrics['hdd_poweronhrs'] = CounterMetricFamily('unvr_hard_disk_poweronhrs', 'NVR Hard Disk Power On Hours', labels=hdd_label_names)
        self.metrics['hdd_temperature'] = GaugeMetricFamily('unvr_hard_disk_temperature', 'NVR Hard Disk Temperature', labels=hdd_label_names)

        self.metrics['storage_health'] = GaugeMetricFamily('unvr_storage_health', 'NVR Storage Health', labels=nvr_common_label_names + ['device', 'health', 'action', 'space_type'])
        
        self.metrics['mem_free'] = GaugeMetricFamily('unvr_memory_free', 'Memory Free', labels=nvr_common_label_names)
        self.metrics['mem_available'] = GaugeMetricFamily('unvr_memory_available', 'Memory Available', labels=nvr_common_label_names)
        self.metrics['mem_total'] = GaugeMetricFamily('unvr_memory_total', 'Memory Total', labels=nvr_common_label_names)
        
        self.metrics['cam_txbytes'] = CounterMetricFamily('unvr_cam_txbytes', 'Camera TX Bytes', labels=cam_common_label_names)
        self.metrics['cam_rxbytes'] = CounterMetricFamily('unvr_cam_rxbytes', 'Camera RX Bytes', labels=cam_common_label_names)
        self.metrics['cam_state'] = GaugeMetricFamily('unvr_cam_state', 'Camera Status', labels=cam_common_label_names + ['cam_state'])
        self.metrics['cam_last_seen'] = GaugeMetricFamily('unvr_cam_last_seen', 'Camera last_seen', labels=cam_common_label_names)
        self.metrics['cam_last_motion'] = GaugeMetricFamily('unvr_cam_last_motion', 'Camera last motion', labels=cam_common_label_names)
        self.metrics['cam_last_disconnect'] = GaugeMetricFamily('unvr_cam_last_disconnect', 'Camera last disconnect', labels=cam_common_label_names)

    def collect(self):
        logging.info(f"Incoming request {self.conf['host']}")
        if time() - self.ts < 15:
            for v in self.metrics.values():
                yield v

    def login(self):
        # start unifi session
        logging.warning(f"Login {self.conf['host']}")
        req = self.session.post(self.conf.get('host') + '/api/auth/login', data={'username': self.conf.get('username'), 'password': self.conf.get('password'), 'remember': True}, verify=False)
        if req.status_code != 200:
            raise Exception(f'Could not login to NVR: {req.text}')

    def get_data(self):
        # Get Bootstrap json
        bootstrap = self.session.get(f"{self.conf.get('host')}/proxy/protect/api/bootstrap")
        if bootstrap.status_code == 401:
            logging.info(f'Got error 401, Performing login')
            self.login()
            return self.get_data()

        elif bootstrap.status_code != 200:
            raise Exception(f'Got Error: {bootstrap.text}')()

        return bootstrap.json()

    def refresh(self):
        err_counter = 0
        while err_counter < 2:
            try:
                j = self.get_data()
                self.get_metrics(j)
                self.ts = time()
                break
        
            except Exception as e:
                err_counter += 1
                logging.error(
                    f'Unable to collect metrics from NVR. {e}\n{traceback.format_exc()}'
                )

    def get_metrics(self, js):
        nvr = js['nvr']
        nvrName = nvr['name']
        nvrHost = nvr['host']

        basic_info = [nvr[key] for key in ['id', 'name', 'host', 'mac']]

        for v in self.metrics.values():
            v.samples.clear()

        # CPU
        self.metrics['cpu_load'].add_metric(labels = basic_info, value = nvr['systemInfo']['cpu']['averageLoad'])
        self.metrics['cpu_temperature'].add_metric(labels = basic_info, value = nvr['systemInfo']['cpu']['temperature'])

        # Hard Disk, what is this?
        st = 0 if nvr.get('hardDriveState') == 'ok' else 2
        self.metrics['hdd_state'].samples.clear()
        self.metrics['hdd_state'].add_metric(labels = basic_info + [nvr['hardDriveState']], value = st)

        # HDD
        for disk in nvr['systemInfo']['ustorage']['disks']:
            v = 0 if disk.get('healthy') == 'good' else 2
            self.metrics['hdd_health'].add_metric(
                labels = basic_info + [str(disk.get(key)) for key in ['slot', 'model', 'healthy', 'state']],
                value = v)
            self.metrics['hdd_size'].add_metric(
                labels = basic_info + [str(disk.get(key)) for key in ['slot', 'model', 'healthy', 'state']],
                value = disk.get('size', 0))
            self.metrics['hdd_poweronhrs'].add_metric(
                labels = basic_info + [str(disk.get(key)) for key in ['slot', 'model', 'healthy', 'state']],
                value = disk.get('poweronhrs', 0))
            self.metrics['hdd_temperature'].add_metric(
                labels = basic_info + [str(disk.get(key)) for key in ['slot', 'model', 'healthy', 'state']],
                value = disk.get('temperature', 0))
        
        for ldisk in nvr['systemInfo']['ustorage']['space']:
            st = 0 if ldisk['health'] == 'health' else 2
            self.metrics['storage_health'].add_metric(labels = basic_info + [ldisk[key] for key in ['device', 'health', 'action', 'space_type']], value = st)
        
        # Memory
        self.metrics['mem_free'].add_metric(labels = basic_info, value = nvr['systemInfo']['memory']['free'])
        self.metrics['mem_available'].add_metric(labels = basic_info, value = nvr['systemInfo']['memory']['available'])
        self.metrics['mem_total'].add_metric(labels = basic_info, value = nvr['systemInfo']['memory']['total'])

        # Cameras
        for cam in js.get('cameras', {}):
            if not cam.get('isAdopted'):
                continue

            camInfo = [nvrName, nvrHost] + [cam[key] for key in ['name', 'host', 'mac']]
            self.metrics['cam_last_seen'].add_metric(labels = camInfo, value = cam['lastSeen'])
            if cam.get('lastMotion'):
                self.metrics['cam_last_motion'].add_metric(labels = camInfo, value = cam['lastMotion'])
            self.metrics['cam_last_disconnect'].add_metric(labels = camInfo, value = cam['lastDisconnect'])

            if cam.get('stats', {}).get('rxBytes'):
                self.metrics['cam_rxbytes'].add_metric(labels = camInfo, value = cam.get('stats', {}).get('rxBytes', 0))
            if cam.get('stats', {}).get('txBytes'):
                self.metrics['cam_txbytes'].add_metric(labels = camInfo, value = cam.get('stats', {}).get('txBytes', 0))
            
            state = -1
            st = cam.get('state')
            try:
                state = ['CONNECTED', 'CONNECTING', 'DISCONNECTED'].index(st)
            except ValueError:
                logging.warning(f'Unknown camera state: {st}')
                pass

            self.metrics['cam_state'].add_metric(labels = camInfo + [st], value = state)


def run_collection(s, collector, interval):
    logging.info(f"Refreshing {collector.conf['host']}")
    s.enter(interval, 1, run_collection, argument=(s, collector, interval))
    collector.refresh()
    logging.info(f'Refresh Done')

if __name__ == '__main__':
    # set config
    logging.basicConfig(encoding='utf-8', level=logging.WARNING)
    requests.packages.urllib3.disable_warnings()
    config = configparser.ConfigParser()
    config.read('config.ini')

    config.get

    s = scheduler()

    # get params from config parser
    server_config = {
        'port': 8222,
        'address': '0.0.0.0'
    }

    collectors = []
    for n, c in config.items():
        if n == 'DEFAULT':
            if c.get('port'):
                server_config['port'] = c.get('port')
            if c.get('address'):
                server_config['address'] = c.get('address')
            continue

        interval = int(c.get('polling_interval', 10))
        use_https = c.getboolean('use_https', True)
        host = c.get('host')
        scheme = 'https://' if use_https else 'http://'
        host = scheme + host
        username = c.get('username')
        password = c.get('password')

        collector = NVRCollector({'host': host, 'username': username, 'password': password})
        REGISTRY.register(collector)
        collectors.append(collector)

    for collector in collectors:
        run_collection(s, collector, interval)

    try:
        server, t = start_http_server(int(server_config.get('port')), server_config.get('address'))
        s.run()
    
    except KeyboardInterrupt:
        server.shutdown()
        t.join(5)
