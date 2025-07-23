#!/usr/bin/python3 -u
#coding: utf8

import requests
import logging
import configparser
import traceback
import sys

from signal import signal, SIGTERM, SIGINT
from sched import scheduler
from time import time, sleep
from prometheus_client import start_http_server
from prometheus_client.context_managers import Timer
from prometheus_client.core import GaugeMetricFamily, CounterMetricFamily, Gauge, Counter, REGISTRY


# Meta Collectors
meta_labels = ['host']
load_time = Counter(f'unvr_data_load_time', 'Total time spent loading metrics in seconds', labelnames=meta_labels)
load_count = Counter(f'unvr_data_load_count', 'Total count of metrics loads since reboot', labelnames=meta_labels)
login_count = Counter(f'unvr_login_count', 'Total Login Count', labelnames=meta_labels)
login_fails = Counter(f'unvr_login_fail_count', 'UNVR Total Login Fail Count', labelnames=meta_labels)
last_run = Gauge(f'unvr_data_load_last_run', 'Last run timestamp of metrics load', labelnames=meta_labels)
error_count = Counter(f'unvr_data_load_errors', 'Data Load Error Count', labelnames=meta_labels)

class NVRCollector(object):
    def __init__(self, conf):
        self.conf = conf
        self.stats_cache = {}
        self.session = requests.Session()
        self.ts = 0

        self.host = self.conf['host']

        # Metrics
        cam_common_label_names = ['name', 'host', 'cameraName', 'cameraHost', 'cameraMac']
        nvr_common_label_names = ['id', 'name', 'host', 'mac']

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
        logging.info(f"Incoming request {self.host}")
        if time() - self.ts < 15:
            for v in self.metrics.values():
                yield v

    def login(self):
        # start unifi session
        login_count.labels(self.host).inc()
        logging.warning(f"Login {self.host}")
        req = self.session.post(self.host + '/api/auth/login', data={'username': self.conf.get('username'), 'password': self.conf.get('password'), 'remember': True}, verify=False)
        if req.status_code != 200:
            login_fails.labels(self.host).inc()
            raise Exception(f'Could not login to NVR: {req.text}')

    def refresh(self):
        err_counter = 0
        with Timer(load_time.labels(self.host), 'inc'), error_count.labels(self.host).count_exceptions():
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
        load_count.labels(self.host).inc()
        last_run.labels(self.host).set_to_current_time()

    def get_data(self):
        # Get Bootstrap json
        bootstrap = self.session.get(f"{self.host}/proxy/protect/api/bootstrap")
        if bootstrap.status_code == 401:
            logging.info(f'Got error 401, Performing login')
            self.login()
            return self.get_data()

        elif bootstrap.status_code != 200:
            raise Exception(f'Got Error: {bootstrap.text}')()

        return bootstrap.json()

    def get_metrics(self, js):
        nvr = js['nvr']
        nvrName = nvr['name']
        nvrHost = nvr['host']

        basic_info = [nvr.get(key) for key in ['id', 'name', 'host', 'mac']]

        for v in self.metrics.values():
            v.samples.clear()

        # CPU
        self.add_metric('cpu_load', basic_info, nvr.get('systemInfo', {}).get('cpu', {}).get('averageLoad'))
        self.add_metric('cpu_temperature', basic_info, nvr.get('systemInfo', {}).get('cpu', {}).get('temperature'))

        # Hard Disk, what is this?
        self.add_metric('hdd_state', basic_info + [nvr['hardDriveState']], 0 if nvr.get('hardDriveState') == 'ok' else 2)

        # HDD
        for disk in nvr.get('systemInfo', {}).get('ustorage', {}).get('disks', []):
            label_values = basic_info + [str(disk.get(key)) for key in ['slot', 'model', 'healthy', 'state']]

            self.add_metric('hdd_health', label_values, 0 if disk.get('healthy') == 'good' else 2)
            self.add_metric('hdd_size', label_values, disk.get('size', 0))
            self.add_metric('hdd_poweronhrs', label_values, disk.get('poweronhrs', 0))
            self.add_metric('hdd_temperature', label_values, disk.get('temperature', 0))

        for ldisk in nvr.get('systemInfo', {}).get('ustorage', {}).get('space', []):
            self.add_metric('storage_health', basic_info + [ldisk.get(key) for key in ['device', 'health', 'action', 'space_type']], 0 if ldisk.get('health') == 'health' else 2)

        # Memory
        self.add_metric('mem_free', basic_info, nvr.get('systemInfo', {}).get('memory', {}).get('free'))
        self.add_metric('mem_available', basic_info, nvr.get('systemInfo', {}).get('memory', {}).get('available'))
        self.add_metric('mem_total', basic_info, nvr.get('systemInfo', {}).get('memory', {}).get('total'))

        # Cameras
        for cam in js.get('cameras', {}):
            if not cam.get('isAdopted'):
                continue

            camInfo = [nvrName, nvrHost] + [cam[key] for key in ['name', 'host', 'mac']]
            self.add_metric('cam_last_seen', camInfo, cam.get('lastSeen', 0))
            self.add_metric('cam_last_motion', camInfo, cam['lastMotion'])
            self.add_metric('cam_last_disconnect', camInfo, cam['lastDisconnect'])

            self.add_metric('cam_rxbytes', camInfo, cam.get('stats', {}).get('rxBytes', 0))
            self.add_metric('cam_txbytes', camInfo, cam.get('stats', {}).get('txBytes', 0))
            
            state = -1
            st = cam.get('state')
            try:
                state = ['CONNECTED', 'CONNECTING', 'DISCONNECTED'].index(st)
            except ValueError:
                logging.warning(f'Unknown camera state: {st}')
                pass

            self.add_metric('cam_state', camInfo + [st], state)


    def add_metric(self, name, labels, value):
        if value:
            self.metrics[name].add_metric(labels = labels, value = value)

class ExportProcessor(object):
    def __init__(self):
        signal(SIGINT, self.exit_gracefully)
        signal(SIGTERM, self.exit_gracefully)

        # set config
        logging.basicConfig(encoding='utf-8', level=logging.WARNING)
        requests.packages.urllib3.disable_warnings()

        self.config = configparser.ConfigParser()
        self.config.read('config.ini')

        self.s = scheduler(time, sleep)

    def run_collection(self, collector, interval, next_run):
        while next_run < time():
            next_run += interval

        self.s.enterabs(next_run, 1, self.run_collection, argument=(collector, interval, next_run))
    
        logging.info(f"Refreshing {collector.conf['host']}")
        collector.refresh()
        logging.info(f'Refresh Done')

    def exit_gracefully(self, signal, _):
        logging.warning(f"Caught signal {signal}, stopping")
        for j in self.s.queue:
            logging.warning(f'Cancelling scheduler job')
            self.s.cancel(j)

        logging.info(f'Shut Down HTTP server')
        if self.server:
            self.server.shutdown()

        if self.thr:
            self.thr.join(5)

        logging.info(f'Shut Down Done')
        sys.exit(1)


    def start(self):
        # get params from config parser
        server_config = {
            'port': 8222,
            'address': '0.0.0.0'
        }

        collectors = []
        for n, c in self.config.items():
            if n == 'DEFAULT':
                if c.get('port'):
                    server_config['port'] = c.get('port')
                if c.get('address'):
                    server_config['address'] = c.get('address')
                continue

            interval = int(c.get('polling_interval', 10))
            start_time = round(time(), -1) + interval

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
            self.s.enterabs(start_time, 1, self.run_collection, argument=(collector, interval, start_time))

        self.server, self.thr = start_http_server(int(server_config.get('port')), server_config.get('address'))
        self.s.run()

if __name__ == '__main__':
    ExportProcessor().start()
