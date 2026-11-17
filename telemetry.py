"""Small, read-only Windows telemetry collector for the Galaxy Book widget.

Only psutil and the Windows APIs already present on the machine are used.
No processes, WMI providers, drivers, or registry values are created.  The
display can call sample() periodically; battery and mode reads are cached for
five seconds.  Unsupported readings are None, never made-up zeros.
"""
from __future__ import annotations

import argparse
from collections import deque
import ctypes
from ctypes import wintypes
from datetime import datetime
import json
import math
import os
import re
import time
import urllib.error
import urllib.request
import winreg

import psutil


_INVALID_HANDLE = ctypes.c_void_p(-1).value
_U32 = ctypes.c_uint32
_I32 = ctypes.c_int32


class _Guid(ctypes.Structure):
    _fields_ = [("Data1", _U32), ("Data2", ctypes.c_uint16),
                ("Data3", ctypes.c_uint16), ("Data4", ctypes.c_ubyte * 8)]


class _InterfaceData(ctypes.Structure):
    _fields_ = [("cbSize", _U32), ("InterfaceClassGuid", _Guid),
                ("Flags", _U32), ("Reserved", ctypes.c_size_t)]


class _BatteryQueryInformation(ctypes.Structure):
    _fields_ = [("BatteryTag", _U32), ("InformationLevel", _U32),
                ("AtRate", _I32)]


class _BatteryInformation(ctypes.Structure):
    _fields_ = [("Capabilities", _U32), ("Technology", ctypes.c_ubyte),
                ("Reserved", ctypes.c_ubyte * 3),
                ("Chemistry", ctypes.c_ubyte * 4),
                ("DesignedCapacity", _U32), ("FullChargedCapacity", _U32),
                ("DefaultAlert1", _U32), ("DefaultAlert2", _U32),
                ("CriticalBias", _U32), ("CycleCount", _U32)]


class _BatteryWaitStatus(ctypes.Structure):
    _fields_ = [("BatteryTag", _U32), ("Timeout", _U32),
                ("PowerState", _U32), ("LowCapacity", _U32),
                ("HighCapacity", _U32)]


class _BatteryStatus(ctypes.Structure):
    _fields_ = [("PowerState", _U32), ("Capacity", _U32),
                ("Voltage", _U32), ("Rate", _I32)]


class _PdhValueUnion(ctypes.Union):
    _fields_ = [("longValue", _I32), ("doubleValue", ctypes.c_double),
                ("largeValue", ctypes.c_longlong), ("stringValue", ctypes.c_void_p)]


class _PdhValue(ctypes.Structure):
    _anonymous_ = ("value",)
    _fields_ = [("CStatus", _U32), ("value", _PdhValueUnion)]


def _bind(dll, name, restype, argtypes):
    fn = getattr(dll, name)
    fn.restype = restype
    fn.argtypes = argtypes
    return fn


class _WindowsBattery:
    """Read battery class IOCTLs. A tag is re-acquired on each slow poll."""

    _TAG = 0x00294040
    _INFO = 0x00294044
    _STATUS = 0x0029404C
    _RELATIVE = 0x40000000
    _UNKNOWN_RATE = -2147483648
    _GUID = _Guid(0x72631E54, 0x78A4, 0x11D0,
                  (ctypes.c_ubyte * 8)(0xBC, 0xF7, 0x00, 0xAA, 0x00, 0xB7, 0xB3, 0x2A))

    def __init__(self):
        self.handle = None
        self.k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        self.setup = ctypes.WinDLL("setupapi", use_last_error=True)
        _bind(self.k32, "CreateFileW", wintypes.HANDLE,
              [wintypes.LPCWSTR, _U32, _U32, ctypes.c_void_p, _U32, _U32, wintypes.HANDLE])
        _bind(self.k32, "CloseHandle", wintypes.BOOL, [wintypes.HANDLE])
        _bind(self.k32, "DeviceIoControl", wintypes.BOOL,
              [wintypes.HANDLE, _U32, ctypes.c_void_p, _U32, ctypes.c_void_p,
               _U32, ctypes.POINTER(_U32), ctypes.c_void_p])
        _bind(self.setup, "SetupDiGetClassDevsW", wintypes.HANDLE,
              [ctypes.POINTER(_Guid), wintypes.LPCWSTR, wintypes.HWND, _U32])
        _bind(self.setup, "SetupDiEnumDeviceInterfaces", wintypes.BOOL,
              [wintypes.HANDLE, ctypes.c_void_p, ctypes.POINTER(_Guid), _U32,
               ctypes.POINTER(_InterfaceData)])
        _bind(self.setup, "SetupDiGetDeviceInterfaceDetailW", wintypes.BOOL,
              [wintypes.HANDLE, ctypes.POINTER(_InterfaceData), ctypes.c_void_p,
               _U32, ctypes.POINTER(_U32), ctypes.c_void_p])
        _bind(self.setup, "SetupDiDestroyDeviceInfoList", wintypes.BOOL, [wintypes.HANDLE])

    def _open(self):
        devices = self.setup.SetupDiGetClassDevsW(ctypes.byref(self._GUID), None, None, 0x12)
        if devices == _INVALID_HANDLE:
            return False
        try:
            for index in range(8):
                entry = _InterfaceData()
                entry.cbSize = ctypes.sizeof(entry)
                if not self.setup.SetupDiEnumDeviceInterfaces(devices, None, ctypes.byref(self._GUID), index, ctypes.byref(entry)):
                    break
                required = _U32()
                self.setup.SetupDiGetDeviceInterfaceDetailW(devices, ctypes.byref(entry), None, 0, ctypes.byref(required), None)
                if not required.value or required.value > 65536:
                    continue
                detail = ctypes.create_string_buffer(required.value)
                ctypes.cast(detail, ctypes.POINTER(_U32)).contents.value = 8 if ctypes.sizeof(ctypes.c_void_p) == 8 else 6
                if not self.setup.SetupDiGetDeviceInterfaceDetailW(devices, ctypes.byref(entry), detail, required.value, ctypes.byref(required), None):
                    continue
                # DevicePath begins immediately after the DWORD cbSize on both architectures.
                path = ctypes.wstring_at(ctypes.addressof(detail) + 4)
                h = self.k32.CreateFileW(path, 0x80000000, 0x3, None, 3, 0x80, None)
                if h != _INVALID_HANDLE:
                    self.handle = h
                    return True
        finally:
            self.setup.SetupDiDestroyDeviceInfoList(devices)
        return False

    def _ioctl(self, code, inp, out):
        returned = _U32()
        return bool(self.k32.DeviceIoControl(self.handle, code, ctypes.byref(inp), ctypes.sizeof(inp),
                                             ctypes.byref(out), ctypes.sizeof(out), ctypes.byref(returned), None))

    def read(self):
        if self.handle is None and not self._open():
            return None
        timeout = _U32(0)
        tag = _U32()
        if not self._ioctl(self._TAG, timeout, tag) or not tag.value:
            self.close()
            return None
        info_request = _BatteryQueryInformation(tag.value, 0, 0)
        info = _BatteryInformation()
        if not self._ioctl(self._INFO, info_request, info):
            return None
        request = _BatteryWaitStatus(tag.value, 0, 0, 0, 0)
        status = _BatteryStatus()
        if not self._ioctl(self._STATUS, request, status):
            return None
        is_relative = bool(info.Capabilities & self._RELATIVE)
        watts = None if is_relative or status.Rate == self._UNKNOWN_RATE else abs(status.Rate) / 1000.0
        return {"power_state": status.PowerState, "watts": watts,
                "rate": None if status.Rate == self._UNKNOWN_RATE else status.Rate,
                "capacity_mwh": None if is_relative or status.Capacity == 0xFFFFFFFF else status.Capacity,
                "relative": is_relative}

    def close(self):
        if self.handle is not None:
            self.k32.CloseHandle(self.handle)
            self.handle = None


class _CpuFrequency:
    """PDH Actual Frequency counter (MHz); no nominal-frequency fallback."""

    def __init__(self):
        self.pdh = ctypes.WinDLL("pdh", use_last_error=True)
        _bind(self.pdh, "PdhOpenQueryW", _I32, [wintypes.LPCWSTR, ctypes.c_size_t, ctypes.POINTER(wintypes.HANDLE)])
        _bind(self.pdh, "PdhAddEnglishCounterW", _I32, [wintypes.HANDLE, wintypes.LPCWSTR, ctypes.c_size_t, ctypes.POINTER(wintypes.HANDLE)])
        _bind(self.pdh, "PdhCollectQueryData", _I32, [wintypes.HANDLE])
        _bind(self.pdh, "PdhGetFormattedCounterValue", _I32,
              [wintypes.HANDLE, _U32, ctypes.c_void_p, ctypes.POINTER(_PdhValue)])
        _bind(self.pdh, "PdhCloseQuery", _I32, [wintypes.HANDLE])
        self.query = wintypes.HANDLE()
        self.counter = wintypes.HANDLE()
        if self.pdh.PdhOpenQueryW(None, 0, ctypes.byref(self.query)) != 0:
            self.query = None
            return
        # Processor Frequency is a reference value on this laptop and remains
        # unchanged; Actual Frequency is the changing, sampled value.
        path = r"\Processor Information(_Total)\Actual Frequency"
        if self.pdh.PdhAddEnglishCounterW(self.query, path, 0, ctypes.byref(self.counter)) != 0:
            self.close()

    def read(self):
        if self.query is None or self.pdh.PdhCollectQueryData(self.query) != 0:
            return None
        result = _PdhValue()
        if self.pdh.PdhGetFormattedCounterValue(self.counter, 0x200, None, ctypes.byref(result)) != 0:
            return None
        if result.CStatus not in (0, 1) or not math.isfinite(result.doubleValue) or not 100 <= result.doubleValue <= 10000:
            return None
        return result.doubleValue / 1000.0

    def close(self):
        if self.query:
            self.pdh.PdhCloseQuery(self.query)
        self.query = None


def _mode():
    try:
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Samsung\SamsungSettings\ModulePerformance", 0, winreg.KEY_READ | winreg.KEY_WOW64_64KEY) as key:
            value, _ = winreg.QueryValueEx(key, "Value")
        return {3: "고성능", 2: "최적화", 1: "저소음", 0: "조용함"}.get(value)
    except OSError:
        return None


def _eligible_interfaces():
    # Avoid double-counting loopback and common overlay/VPN/virtual interfaces.
    exclude = ("loopback", "bluetooth", "tailscale", "virtual", "vethernet", "vEthernet".lower(),
               "tunnel", "wireguard", "wintun", "zerotier", "vmware", "hyper-v", "wi-fi direct")
    stats = psutil.net_if_stats()
    return {name for name, info in stats.items()
            if info.isup and not any(piece in name.lower() for piece in exclude)}


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, msg, headers, newurl):
        return None


class _LibreSensors:
    """Optional existing local LibreHardwareMonitor web server, read-only.

    The URL must be explicitly configured, local, fixed-port and exact-path.
    Requests bypass proxies and redirects and are capped in time and size.
    """
    ALLOWED = {"http://127.0.0.1:8085/data.json", "http://localhost:8085/data.json"}

    def __init__(self, url):
        if url not in self.ALLOWED:
            raise ValueError("sensor URL must be the local LibreHardwareMonitor endpoint")
        # localhost may resolve unexpectedly through custom DNS/hosts; pin the
        # network destination to IPv4 loopback regardless of the spelling used.
        self.url = "http://127.0.0.1:8085/data.json"
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())

    @staticmethod
    def _number(value):
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            number = float(value)
        elif isinstance(value, str):
            # Sensor values use either a comma or dot as a decimal separator.
            normalized = value.replace("\u00a0", " ")
            # RPM values are whole numbers; account for a thousands separator.
            if "rpm" in normalized.lower():
                normalized = re.sub(r"(?<=\d),(?=\d{3}(?:\D|$))", "", normalized)
            match = re.search(r"[-+]?\d+(?:[.,]\d+)?", normalized)
            if not match:
                return None
            number = float(match.group().replace(",", "."))
        else:
            return None
        return number if math.isfinite(number) else None

    @classmethod
    def parse(cls, root):
        found = {"cpu_temp_c": None, "cpu_power_w": None, "fan_rpm": None}
        scores = {key: -1 for key in found}

        def visit(node, ancestry=()):
            if not isinstance(node, dict) or len(ancestry) > 24:
                return
            text = str(node.get("Text") or "")
            sensor_id = str(node.get("SensorId") or "")
            sensor_type = str(node.get("Type") or "").lower()
            raw = node.get("Value")
            number = cls._number(raw)
            branch = " / ".join((*ancestry, text, sensor_id)).lower()
            label = text.lower()
            value_text = str(raw).lower()
            is_cpu = (any(x in branch for x in ("intelcpu", "intel core", "cpu package", "processor", "amd cpu", "amd ryzen"))
                      or ("intel" in branch and "core" in branch))
            is_gpu = any(x in branch for x in ("gpu", "geforce", "radeon", "nvidia"))
            is_temp = ("temperature" in sensor_type or "temperature" in branch
                       or "°c" in value_text or "℃" in value_text)
            is_power = "power" in sensor_type or "powers" in branch or value_text.rstrip().endswith(" w")
            is_fan = "fan" in sensor_type or "fans" in branch or "rpm" in value_text
            if number is not None:
                if is_cpu and not is_gpu and is_temp and -10 <= number <= 125:
                    rank = 5 if "package" in label else 4 if "core average" in label else 3 if "tctl" in label else 1
                    if rank > scores["cpu_temp_c"]:
                        found["cpu_temp_c"], scores["cpu_temp_c"] = number, rank
                if is_cpu and not is_gpu and is_power and 0 <= number <= 500:
                    # Only package power is comparable; never mistake a core,
                    # PL1 limit, or nominal adapter wattage for CPU power.
                    rank = 5 if "package" in label else -1
                    if rank > scores["cpu_power_w"]:
                        found["cpu_power_w"], scores["cpu_power_w"] = number, rank
                if is_fan and not is_gpu and 0 <= number <= 30000:
                    rank = 5 if "cpu" in label else 3 if "fan" in label else 1
                    if rank > scores["fan_rpm"]:
                        found["fan_rpm"], scores["fan_rpm"] = int(number), rank
            for child in node.get("Children") or []:
                visit(child, (*ancestry, text, sensor_id))

        visit(root)
        return found

    def read(self):
        try:
            with self.opener.open(self.url, timeout=0.35) as response:
                if response.status != 200:
                    return None
                body = response.read(1024 * 1024 + 1)
                if len(body) > 1024 * 1024:
                    return None
                return self.parse(json.loads(body))
        except (OSError, ValueError, urllib.error.URLError, RecursionError):
            return None


def _battery_seconds(value, raw=None):
    # GetSystemPowerStatus uses an unsigned DWORD. Some psutil/Windows pairs
    # expose UNKNOWN (0xFFFFFFFF) as 4294967295 rather than -1.
    def valid(seconds):
        return (isinstance(seconds, (int, float)) and math.isfinite(seconds)
                and 0 < seconds <= 7 * 24 * 3600)
    if valid(value):
        return int(value)
    if raw and not raw.get("relative"):
        capacity, rate = raw.get("capacity_mwh"), raw.get("rate")
        if (isinstance(capacity, (int, float)) and math.isfinite(capacity) and capacity >= 0
                and isinstance(rate, (int, float)) and math.isfinite(rate)
                and rate < 0 and rate != -2147483648):
            estimate = capacity / abs(rate) * 3600
            if valid(estimate):
                return int(estimate)
    return None


class RemainingTimeEstimator:
    """Time-weighted 90-second draw average, published at most every 30 seconds."""

    def __init__(self):
        self.reset()

    def reset(self):
        self.samples = deque(maxlen=64)
        self.source = None
        self.result = None
        self.published_at = None

    def update(self, now, raw=None, fallback_seconds=None):
        capacity = draw = None
        source = None
        if raw and not raw.get("relative"):
            cap, rate = raw.get("capacity_mwh"),raw.get("rate")
            if (isinstance(cap,(int,float)) and math.isfinite(cap) and cap > 0
                    and isinstance(rate,(int,float)) and math.isfinite(rate)
                    and rate < 0 and rate != -2147483648):
                capacity,draw,source = cap,abs(rate),"battery_rate"
        if source is None:
            seconds = _battery_seconds(fallback_seconds)
            if seconds:
                capacity,draw,source = 1.0,3600.0/seconds,"windows_estimate"
        if source is None:
            self.reset()
            return None
        if (source != self.source or (self.samples and
                (now <= self.samples[-1][0] or now-self.samples[-1][0] > 30))):
            self.reset()
        self.source = source
        self.samples.append((now,draw))
        cutoff = now-90
        while len(self.samples) > 2 and self.samples[1][0] <= cutoff:
            self.samples.popleft()
        if len(self.samples) < 3 or now-self.samples[0][0] < 10:
            return None
        if self.published_at is not None and now-self.published_at < 30:
            return self.result
        integral = coverage = 0.0
        for (t0,p0),(t1,p1) in zip(self.samples,list(self.samples)[1:]):
            start,end = max(t0,cutoff),min(t1,now)
            if end <= start or t1 <= t0:
                continue
            left = p0+(p1-p0)*(start-t0)/(t1-t0)
            right = p0+(p1-p0)*(end-t0)/(t1-t0)
            integral += (left+right)*0.5*(end-start)
            coverage += end-start
        average = integral/coverage if coverage else 0
        self.result = _battery_seconds(capacity/average*3600) if average > 0 else None
        self.published_at = now
        return self.result


class Collector:
    """Persistent read-only collector. Call sample() every ~2 seconds and close()."""

    def __init__(self, sensor_url=None):
        self.battery = _WindowsBattery()
        self.cpu_frequency = _CpuFrequency()
        self.sensors = _LibreSensors(sensor_url) if sensor_url else None
        self.slow_sample = {}
        self.last_slow = -float("inf")
        self.last_ifaces = -float("inf")
        self.ifaces = set()
        self.network_prev = None
        self.cpu_primed = False
        self.remaining_time = RemainingTimeEstimator()

    def _read_slow(self):
        errors = {}
        data = {"battery_percent": None, "battery_state": "unknown", "battery_seconds": None,
                "battery_watts": None, "battery_power_direction": "unknown", "ac_connected": None,
                "performance_mode": _mode()}
        if data["performance_mode"] is None:
            errors["performance_mode"] = "unavailable"
        try:
            battery = psutil.sensors_battery()
            if battery is None:
                data["battery_state"] = "absent"
                self.remaining_time.reset()
            else:
                data["battery_percent"] = battery.percent
                data["ac_connected"] = battery.power_plugged
                data["battery_state"] = "plugged" if battery.power_plugged else "discharging"
                raw = self.battery.read()
                if raw is not None:
                    state = raw["power_state"]
                    data["ac_connected"] = bool(state & 1)
                    if state & 4:
                        data["battery_state"] = "charging"
                        data["battery_power_direction"] = "charging"
                    elif state & 2:
                        data["battery_state"] = "discharging"
                        data["battery_power_direction"] = "discharging"
                    elif state & 1:
                        data["battery_state"] = "plugged"
                        data["battery_power_direction"] = "idle"
                    data["battery_watts"] = raw["watts"]
                    if raw["watts"] is None:
                        errors["battery_watts"] = "unavailable"
                else:
                    errors["battery_watts"] = "unavailable"
                if data["battery_state"] == "discharging":
                    data["battery_seconds"] = self.remaining_time.update(time.monotonic(),raw,battery.secsleft)
                else:
                    self.remaining_time.reset()
        except (OSError, psutil.Error, ValueError) as exc:
            self.remaining_time.reset()
            errors["battery"] = type(exc).__name__
        sensor_data = self.sensors.read() if self.sensors else None
        for name in ("cpu_temp_c", "cpu_power_w", "fan_rpm"):
            data[name] = sensor_data.get(name) if sensor_data else None
            if data[name] is None:
                errors[name] = "sensor_unavailable" if not self.sensors else "sensor_not_reported" if sensor_data is not None else "sensor_disconnected"
        data["errors"] = errors
        return data

    def sample(self):
        now = time.monotonic()
        if now - self.last_slow >= 5:
            self.slow_sample = self._read_slow()
            self.last_slow = now
        data = dict(self.slow_sample)
        data["errors"] = dict(data.get("errors", {}))
        data.update({"cpu_percent": None, "cpu_ghz": None, "ram_percent": None,
                     "ram_used_gb": None, "ram_total_gb": None, "download_bps": None,
                     "upload_bps": None, "input_power_w": None,
                     "sampled_at": datetime.now().astimezone().isoformat(timespec="seconds"),
                     "monotonic": now})
        data["errors"]["input_power_w"] = "sensor_unavailable"
        try:
            cpu = psutil.cpu_percent(interval=None)
            data["cpu_percent"] = cpu if self.cpu_primed else None
            self.cpu_primed = True
            data["cpu_ghz"] = self.cpu_frequency.read()
            if data["cpu_ghz"] is None:
                data["errors"]["cpu_ghz"] = "unavailable"
        except (OSError, psutil.Error, ValueError) as exc:
            data["errors"]["cpu"] = type(exc).__name__
        try:
            mem = psutil.virtual_memory()
            data.update(ram_percent=mem.percent, ram_used_gb=mem.used / (1024 ** 3),
                        ram_total_gb=mem.total / (1024 ** 3))
        except (OSError, psutil.Error, ValueError) as exc:
            data["errors"]["ram"] = type(exc).__name__
        try:
            if now - self.last_ifaces >= 30:
                self.ifaces = _eligible_interfaces()
                self.last_ifaces = now
            nic = psutil.net_io_counters(pernic=True, nowrap=True)
            names = self.ifaces.intersection(nic)
            incoming = sum(nic[name].bytes_recv for name in names)
            outgoing = sum(nic[name].bytes_sent for name in names)
            if self.network_prev is not None:
                old_time, old_names, old_in, old_out = self.network_prev
                elapsed = now - old_time
                if 0 < elapsed <= 15 and names == old_names and incoming >= old_in and outgoing >= old_out:
                    data["download_bps"] = (incoming - old_in) / elapsed
                    data["upload_bps"] = (outgoing - old_out) / elapsed
            self.network_prev = (now, names, incoming, outgoing)
        except (OSError, psutil.Error, ValueError) as exc:
            self.network_prev = None
            data["errors"]["network"] = type(exc).__name__
        return data

    def close(self):
        self.battery.close()
        self.cpu_frequency.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Read-only local telemetry probe")
    parser.add_argument("--probe", action="store_true", help="print a second sample after two seconds")
    parser.add_argument("--sensor-url", choices=sorted(_LibreSensors.ALLOWED), help="optional existing local LibreHardwareMonitor web server")
    args = parser.parse_args()
    collector = Collector(sensor_url=args.sensor_url)
    try:
        result = collector.sample()
        if args.probe:
            time.sleep(2)
            result = collector.sample()
        print(json.dumps(result, ensure_ascii=False, indent=2))
    finally:
        collector.close()
