import requests
import time
import logging
from logging.handlers import RotatingFileHandler
from opcua import Server, ua
import configparser
import unicodedata
import urllib3
import os

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

def make_safe_name(name):
    trans = (("ä", "ae"), ("ö", "oe"), ("ü", "ue"), ("Ä", "Ae"), ("Ö", "Oe"), ("Ü", "Ue"), ("ß", "ss"))
    for a, b in trans:
        name = name.replace(a, b)
    name = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode("ascii")
    name = name.replace(" ", "_").replace("/", "_")
    return name

# --- Status-Byte Mapping und Hierarchie ---
STATUS_BYTE_MAP = {
    "up": 1, "warning": 2, "down": 0, "unusual": 3, "collecting": 4, "none": 5, "unknown": 6,
    "paused_by_user": 7, "paused_by_schedule": 8, "paused_by_license": 9, "paused_by_parent": 10,
    "paused_by_dependency": 11, "paused_by_user_until": 12, "probe_disconnected": 13,
    "down_acknowledged": 14, "down_partial": 15, "total": 16
}
STATUS_PRIORITY_ORDER = [
    ("down", 0), ("down_partial", 15), ("down_acknowledged", 14), ("paused_by_user", 7),
    ("paused_by_schedule", 8), ("paused_by_license", 9), ("paused_by_parent", 10),
    ("paused_by_dependency", 11), ("probe_disconnected", 13), ("warning", 2), ("unusual", 3),
    ("collecting", 4), ("none", 5), ("unknown", 6), ("up", 1), ("total", 16)
]

def get_status_summary_byte(sensor_list):
    stat_counter = {k: 0 for k in STATUS_BYTE_MAP}
    for sensor in sensor_list:
        status = str(sensor.get("status", "unknown")).lower()
        stat_counter[status] = stat_counter.get(status, 0) + 1
    for key, byte in STATUS_PRIORITY_ORDER:
        if stat_counter.get(key, 0) > 0:
            return byte
    return 6  # unknown

# --- Config laden ---
config = configparser.ConfigParser()
script_dir = os.path.dirname(os.path.abspath(__file__))
config.read(os.path.join(script_dir, 'config.ini'))

BASE_URL = config['prtg']['base_url']
USERNAME = config['prtg']['username']
PASSWORD = config['prtg']['password']
DEVICE_TAG = config['prtg']['device_tag']
ENDPOINT = config['opcua']['endpoint']
NAMESPACE_URI = config['opcua']['namespace_uri']
POLL_INTERVAL = int(config['service']['poll_interval'])
LOG_FILE = config['logging']['log_file']
LOG_LEVEL = getattr(logging, config['logging']['log_level'].upper())
MAX_BYTES = int(config['logging']['max_bytes'])
BACKUP_COUNT = int(config['logging']['backup_count'])

# --- Logging Setup ---
logger = logging.getLogger("prtg_opcua")
logger.setLevel(LOG_LEVEL)
handler = RotatingFileHandler(LOG_FILE, maxBytes=MAX_BYTES, backupCount=BACKUP_COUNT)
formatter = logging.Formatter('%(asctime)s %(levelname)s %(message)s')
handler.setFormatter(formatter)
logger.addHandler(handler)

# --- PRTG API Client ---
class PRTGApiClient:
    def __init__(self, base_url, username, password):
        self.base_url = base_url.rstrip("/")
        self.username = username
        self.password = password
        self.token = None

    def login(self):
        url = f"{self.base_url}/api/v2/session"
        headers = {"accept": "application/json", "Content-Type": "application/json"}
        payload = {"username": self.username, "password": self.password}
        logger.info(f"API-Login: POST {url}")
        resp = requests.post(url, headers=headers, json=payload, verify=False, timeout=15)
        resp.raise_for_status()
        data = resp.json()
        self.token = data.get("token") or data.get("session")
        if not self.token:
            logger.error(f"Kein Token in der Login-Antwort erhalten! Antwort: {data}")
            raise Exception("Kein Token in der Login-Antwort erhalten")
        logger.debug(f"API-Login: Token erhalten: {self.token[:8]}... (gekürzt)")
        return self.token

    def get_sensors_by_tag(self, tag):
        if not self.token:
            self.login()
        url = f"{self.base_url}/api/v2/experimental/sensors"
        params = {
            "offset": 0,
            "limit": 2000,
            "filter": f'basic.parenttags contains "{tag}"'
        }
        headers = {
            "accept": "application/json",
            "Authorization": f"Bearer {self.token}"
        }
        logger.info(f"API-Request: GET {url} Params: {params}")
        resp = requests.get(url, params=params, headers=headers, verify=False, timeout=20)
        resp.raise_for_status()
        sensors = resp.json()
        logger.info(f"API: {len(sensors)} Sensoren mit parenttag '{tag}' gefunden.")
        return sensors

def fetch_devices_and_sensors():
    client = PRTGApiClient(BASE_URL, USERNAME, PASSWORD)
    sensors = client.get_sensors_by_tag(DEVICE_TAG)
    devices = {}
    for sensor in sensors:
        parent = sensor.get("parent", {})
        dev_id = str(parent.get("id"))
        dev_name = parent.get("name", "Unknown Device")
        if dev_id not in devices:
            devices[dev_id] = {
                "id": dev_id,
                "name": dev_name,
                "sensors": []
            }
        devices[dev_id]["sensors"].append(sensor)
    logger.info(f"Gruppiert: {len(devices)} Geräte mit Sensoren.")
    return list(devices.values())
class OPCUAService:
    def __init__(self):
        self.server = Server()
        self.server.set_endpoint(ENDPOINT)
        self.idx = self.server.register_namespace(NAMESPACE_URI)
        self.objects = self.server.nodes.objects
        self.device_nodes = {}

    def setup_devices(self, devices):
        for dev in devices:
            dev_id = str(dev['id'])
            dev_name = dev['name']
            safe_name = make_safe_name(dev_name)
            device_node_name = f"Device_{dev_id}_{safe_name}"
            if dev_id not in self.device_nodes:
                node = self.objects.add_object(self.idx, device_node_name)
                node.add_variable(self.idx, "StatusSummary", ua.Variant(0, ua.VariantType.Byte))
                self.device_nodes[dev_id] = node
                logger.info(f"Device added: {dev_name} (ID: {dev_id}) als OPCUA Node: {device_node_name}")

            # Sensoren als Child-Nodes hinzufügen (statisch über IDs)
            if not hasattr(self.device_nodes[dev_id], "sensor_nodes"):
                self.device_nodes[dev_id].sensor_nodes = {}
            sensor_nodes = self.device_nodes[dev_id].sensor_nodes
            for sensor in dev.get("sensors", []):
                sensor_id = str(sensor['id'])
                sensor_name = make_safe_name(sensor.get('name', f"Sensor_{sensor_id}"))
                sensor_node_name = f"Sensor_{sensor_id}_{sensor_name}"
                if sensor_id not in sensor_nodes:
                    sensor_node = self.device_nodes[dev_id].add_object(self.idx, sensor_node_name)
                    status_val = str(sensor.get('status', 'unknown')).lower()
                    status_byte = int(STATUS_BYTE_MAP.get(status_val, 6))
                    sensor_node.add_variable(self.idx, "status", ua.Variant(status_byte, ua.VariantType.Byte))
                    sensor_node.add_variable(self.idx, "message", str(sensor.get("message", "")))
                    sensor_node.add_variable(self.idx, "uptimetotal", int(sensor.get("uptimetotal", 0)))
                    sensor_nodes[sensor_id] = sensor_node

    def update_devices(self, devices):
        for dev in devices:
            dev_id = str(dev['id'])
            node = self.device_nodes.get(dev_id)
            if node:
                summary_byte = int(get_status_summary_byte(dev.get("sensors", [])))
                for v in node.get_variables():
                    if v.get_display_name().Text == "StatusSummary":
                        v.set_value(ua.Variant(summary_byte, ua.VariantType.Byte))
                sensor_nodes = getattr(node, "sensor_nodes", {})
                for sensor in dev.get("sensors", []):
                    sensor_id = str(sensor['id'])
                    sensor_node = sensor_nodes.get(sensor_id)
                    if sensor_node:
                        status_val = str(sensor.get('status', 'unknown')).lower()
                        status_byte = int(STATUS_BYTE_MAP.get(status_val, 6))
                        for var in sensor_node.get_variables():
                            vname = var.get_display_name().Text
                            if vname == "status":
                                var.set_value(ua.Variant(status_byte, ua.VariantType.Byte))
                            elif vname == "message":
                                var.set_value(str(sensor.get("message", "")))
                            elif vname == "uptimetotal":
                                var.set_value(int(sensor.get("uptimetotal", 0)))
            else:
                logger.warning(f"Device with ID {dev_id} not found in device_nodes (should not happen!)")

    def start(self):
        self.server.start()
        logger.info(f"OPC UA Server started on {ENDPOINT}")

    def stop(self):
        self.server.stop()
        logger.info("OPC UA Server stopped")

def print_device_overview(devices, opcua_service):
    lines = []
    lines.append("Gefundene Geräte, Sensoren und OPC UA Pfade:")
    for dev in devices:
        dev_id = str(dev['id'])
        dev_name = dev['name']
        safe_name = make_safe_name(dev_name)
        opcuanode = f"Device_{dev_id}_{safe_name}"
        lines.append(f"  Gerät: '{dev_name}' (ID: {dev_id}), OPC UA Node: {opcuanode}")
        lines.append(f"    StatusSummary -> {opcuanode}.StatusSummary")
        sensor_nodes = getattr(opcua_service.device_nodes[dev_id], "sensor_nodes", {})
        for sensor in dev.get("sensors", []):
            sensor_id = str(sensor['id'])
            sensor_name = make_safe_name(sensor.get('name', f"Sensor_{sensor_id}"))
            sensor_node = f"{opcuanode}.Sensor_{sensor_id}_{sensor_name}"
            status_val = str(sensor.get('status', 'unknown')).lower()
            status_byte = int(STATUS_BYTE_MAP.get(status_val, 6))
            lines.append(f"      Sensor: '{sensor.get('name','')}' (ID: {sensor_id}), OPC UA Node: {sensor_node}")
            lines.append(f"        status -> OPC UA Pfad: {sensor_node}.status, Byte-Wert: {status_byte}")
            lines.append(f"        message -> OPC UA Pfad: {sensor_node}.message")
            lines.append(f"        uptimetotal -> OPC UA Pfad: {sensor_node}.uptimetotal")
    out = "\n".join(lines)
    logger.info(out)
    print(out)

def run_service():
    opcua_service = OPCUAService()
    opcua_service.start()
    try:
        first_run = True
        while True:
            try:
                devices = fetch_devices_and_sensors()
                if devices is None:
                    logger.warning("API-Call returned None!")
                    devices = []
                opcua_service.setup_devices(devices)
                opcua_service.update_devices(devices)
                if not devices:
                    logger.warning("Keine passenden Geräte/Sensoren von der API gefunden.")
                else:
                    logger.info(f"{len(devices)} Geräte verarbeitet.")
                if first_run:
                    print_device_overview(devices, opcua_service)
                    first_run = False
                time.sleep(POLL_INTERVAL)
            except Exception as ex:
                logger.error(f"PRTG/OPCUA update failed: {ex}", exc_info=True)
                time.sleep(10)
                continue
    except Exception as fatal:
        logger.critical(f"Service crashed: {fatal}", exc_info=True)
    finally:
        opcua_service.stop()

if __name__ == "__main__":
    while True:
        try:
            run_service()
        except Exception as restart:
            logger.critical(f"Service restarting after fatal error: {restart}", exc_info=True)
            time.sleep(5)