"""透過 NVML 讀取顯示卡狀態。不需要 torch,讀取一次約 1 毫秒,可每秒輪詢。"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass
class GpuStatus:
    name: str
    driver: str
    mem_total_mb: float
    mem_used_mb: float
    temperature_c: int | None
    utilization_pct: int | None

    @property
    def mem_free_mb(self) -> float:
        return self.mem_total_mb - self.mem_used_mb


class GpuMonitor:
    def __init__(self, index: int = 0) -> None:
        self.index = index
        self._handle = None
        self.error: str | None = None
        try:
            import pynvml

            pynvml.nvmlInit()
            self._nvml = pynvml
            self._handle = pynvml.nvmlDeviceGetHandleByIndex(index)
        except Exception as e:  # 沒有 NVIDIA 驅動、或套件未安裝
            self.error = str(e)

    @property
    def available(self) -> bool:
        return self._handle is not None

    def read(self) -> GpuStatus | None:
        if self._handle is None:
            return None
        n, h = self._nvml, self._handle
        try:
            name = n.nvmlDeviceGetName(h)
            if isinstance(name, bytes):
                name = name.decode()
            driver = n.nvmlSystemGetDriverVersion()
            if isinstance(driver, bytes):
                driver = driver.decode()
            mem = n.nvmlDeviceGetMemoryInfo(h)
            try:
                temp = int(n.nvmlDeviceGetTemperature(h, n.NVML_TEMPERATURE_GPU))
            except Exception:
                temp = None
            try:
                util = int(n.nvmlDeviceGetUtilizationRates(h).gpu)
            except Exception:
                util = None
            return GpuStatus(
                name=name,
                driver=driver,
                mem_total_mb=mem.total / 2**20,
                mem_used_mb=mem.used / 2**20,
                temperature_c=temp,
                utilization_pct=util,
            )
        except Exception as e:
            self.error = str(e)
            return None
