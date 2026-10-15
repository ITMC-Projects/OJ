"""Protocol allowlist shared by monitoring consumers; no arbitrary host payloads."""
import math


METRICS = ('cpu_percent', 'cpu_count', 'memory_total_bytes', 'memory_available_bytes',
           'disk_total_bytes', 'disk_free_bytes', 'network_received_bytes', 'network_sent_bytes')


def clean_metrics(value):
    if not isinstance(value, dict):
        return {}
    return {key: number for key in METRICS
            for number in [value.get(key)]
            if type(number) in (int, float) and 0 <= number <= 2 ** 63 and math.isfinite(number) and
            (key != 'cpu_percent' or number <= 100)}
