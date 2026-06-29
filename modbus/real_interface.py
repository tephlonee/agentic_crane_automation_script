"""
Real Modbus TCP client using pymodbus.
Connect this to the University West crane simulation (127.0.0.1:502).
"""

import logging
from pymodbus.client import ModbusTcpClient
from pymodbus.exceptions import ModbusException

from modbus.interface import ModbusInterface

logger = logging.getLogger(__name__)


class RealModbusInterface(ModbusInterface):

    def __init__(self, host: str = "127.0.0.1", port: int = 502):
        self._host = host
        self._port = port
        self._client: ModbusTcpClient = None

    def connect(self) -> bool:
        self._client = ModbusTcpClient(host=self._host, port=self._port)
        ok = self._client.connect()
        if ok:
            logger.info("[Modbus] Connected to %s:%d", self._host, self._port)
        else:
            logger.error("[Modbus] Could not connect to %s:%d", self._host, self._port)
        return ok

    def disconnect(self):
        if self._client:
            self._client.close()
            logger.info("[Modbus] Disconnected")

    def read_holding_register(self, address: int) -> int:
        try:
            result = self._client.read_holding_registers(address, count=1)
            if result.isError():
                logger.error("[Modbus] HR read error at %d", address)
                return 0
            return result.registers[0]
        except ModbusException as e:
            logger.error("[Modbus] Exception reading HR %d: %s", address, e)
            return 0

    def write_holding_register(self, address: int, value: int) -> bool:
        try:
            result = self._client.write_register(address, value)
            return not result.isError()
        except ModbusException as e:
            logger.error("[Modbus] Exception writing HR %d: %s", address, e)
            return False

    def read_coil(self, address: int) -> bool:
        try:
            result = self._client.read_coils(address, count=1)
            if result.isError():
                return False
            return bool(result.bits[0])
        except ModbusException as e:
            logger.error("[Modbus] Exception reading coil %d: %s", address, e)
            return False

    def write_coil(self, address: int, value: bool) -> bool:
        try:
            result = self._client.write_coil(address, value)
            return not result.isError()
        except ModbusException as e:
            logger.error("[Modbus] Exception writing coil %d: %s", address, e)
            return False

    def read_discrete_input(self, address: int) -> bool:
        try:
            result = self._client.read_discrete_inputs(address, count=1)
            if result.isError():
                return False
            return bool(result.bits[0])
        except ModbusException as e:
            logger.error("[Modbus] Exception reading DI %d: %s", address, e)
            return False

    # ------------------------------------------------------------------
    # Helpers needed by ProcessAgent (real mode must poll DI registers)
    # ------------------------------------------------------------------

    def is_station_failed(self, station_name: str) -> bool:
        return False   # override if simulation sends a fail DI

    def start_processing(self, station_name: str, duration: float = 3.0):
        pass  # Real simulation handles timing internally

    def is_processing_done(self, station_name: str) -> bool:
        return False   # caller should poll the done_di register directly
