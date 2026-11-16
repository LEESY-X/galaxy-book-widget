"""Focused edge-case checks. Does not create windows or change system settings."""
import unittest
import tempfile
from pathlib import Path
from unittest.mock import patch

import widget
from telemetry import _LibreSensors, _battery_seconds


class WidgetFormattingTests(unittest.TestCase):
    def test_compact_default_and_existing_zoom_settings(self):
        with tempfile.TemporaryDirectory() as folder:
            config = Path(folder)/"settings.json"
            with patch.object(widget,"CONFIG",config):
                self.assertEqual(widget.load_settings()["zoom"],0.9)
                config.write_text('{"zoom":1.25,"interval":5}',encoding="utf-8")
                settings = widget.load_settings()
                self.assertEqual(settings["zoom"],1.25)
                self.assertEqual(settings["interval"],5)
                config.write_text('{"zoom":-1}',encoding="utf-8")
                self.assertEqual(widget.load_settings()["zoom"],0.9)

    def test_unsupported_sections_are_hidden(self):
        plan = dict(widget.section_plan({"battery":True,"performance_mode":True,"battery_watts":True}))
        self.assertNotIn("fan", plan)
        self.assertEqual(plan["power"],46)
        self.assertNotIn("battery",dict(widget.section_plan({})))
    def test_unknown_values_are_not_reported_as_zero(self):
        self.assertEqual(widget.percent(None), "—")
        self.assertEqual(widget.speed(None), "—")
        self.assertEqual(widget.speed(0), "0 B/s")
        self.assertEqual(widget.percent(0), "0%")
        self.assertEqual(widget.percent(float("nan")), "—")

    def test_unknown_or_unlimited_battery_time_is_not_a_duration(self):
        for value in (None, -1, -2, 0, 0xFFFFFFFF, 0xFFFFFFFE, 4294967280, float("inf"), float("nan")):
            self.assertIsNone(widget.duration(value))
            self.assertIsNone(_battery_seconds(value))
        self.assertEqual(widget.duration(3900), "약 1시간 05분")

    def test_unknown_windows_time_falls_back_to_capacity_over_rate(self):
        raw = {"capacity_mwh":20000,"rate":-10000,"relative":False}
        self.assertEqual(_battery_seconds(0xFFFFFFFF,raw),7200)
        self.assertEqual(_battery_seconds(3900,raw),3900)
        for rate in (0,10000,-2147483648):
            self.assertIsNone(_battery_seconds(0xFFFFFFFF,dict(raw,rate=rate)))
        self.assertIsNone(_battery_seconds(0xFFFFFFFF,dict(raw,relative=True)))

    def test_signed_power_has_direction_and_no_invented_sign(self):
        self.assertEqual(widget.power_value(18.4,"discharging"),"−18.4 W")
        self.assertEqual(widget.power_value(24,"charging"),"+24.0 W")
        self.assertEqual(widget.power_value(0,"idle"),"0.0 W")
        self.assertEqual(widget.power_value(18.4,"unknown"),"—")

    def test_cpu_package_values_are_not_taken_from_gpu_or_power_limits(self):
        data = {"Text":"Computer", "Children":[
            {"Text":"Intel Core i7-1360P", "Children":[
                {"Text":"Temperatures", "Children":[{"Text":"CPU Package", "Value":"52.5 °C"}]},
                {"Text":"Powers", "Children":[{"Text":"CPU Cores", "Value":"3.0 W"}, {"Text":"CPU Package", "Value":"8.2 W"}, {"Text":"PL1", "Value":"28.0 W"}]}]},
            {"Text":"NVIDIA GPU", "Children":[{"Text":"GPU Core", "Value":"70.0 °C"}, {"Text":"GPU Fan", "Value":"3,200 RPM"}]},
            {"Text":"Motherboard", "Children":[{"Text":"Fans", "Children":[{"Text":"CPU Fan", "Value":"2,150 RPM"}]}]}
        ]}
        parsed = _LibreSensors.parse(data)
        self.assertEqual(parsed["cpu_temp_c"], 52.5)
        self.assertEqual(parsed["cpu_power_w"], 8.2)
        self.assertEqual(parsed["fan_rpm"], 2150)

    def test_disconnected_or_invalid_sensor_payload_stays_unknown(self):
        for data in ({}, None, {"Text":"CPU", "Value":"N/A"}):
            self.assertTrue(all(value is None for value in _LibreSensors.parse(data).values()))
        with self.assertRaises(ValueError):
            _LibreSensors("https://example.com/data.json")


if __name__ == "__main__":
    unittest.main()
